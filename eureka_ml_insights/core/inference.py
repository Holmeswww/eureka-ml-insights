import asyncio
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from tqdm import tqdm

from eureka_ml_insights.data_utils.data import DataReader, JsonLinesWriter

from .pipeline import Component
from .reserved_names import INFERENCE_RESERVED_NAMES

MINUTE = 60


class Inference(Component):
    def __init__(
        self,
        model_config,
        data_config,
        output_dir,
        resume_from=None,
        new_columns=None,
        requests_per_minute=None,
        max_concurrent=1,
        chat_mode=False,
    ):
        """
        Initialize the Inference component.
        args:
            model_config (dict): ModelConfig object.
            data_config (dict): DataSetConfig object.
            output_dir (str): Directory to save the inference results.
            resume_from (str): optional. Path to the file where previous inference results are stored.
            new_columns (list): optional. List of new columns to be added to resume_from data to match the current inference response.
            requests_per_minute (int): optional. Number of inference requests to be made per minute, used for rate limiting. If not provided, rate limiting will not be applied.
            max_concurrent (int): optional. Maximum number of concurrent inferences to run. Default is 1.
            chat_mode (bool): optional. If True, the model will be used in chat mode, where a history of messages will be maintained in "previous_messages" column.
        """
        super().__init__(output_dir)
        self.model = model_config.class_name(**model_config.init_args)
        self.data_loader = data_config.class_name(**data_config.init_args)
        self.writer = JsonLinesWriter(os.path.join(output_dir, "inference_result.jsonl"))

        self.resume_from = resume_from
        if resume_from and not os.path.exists(resume_from):
            raise FileNotFoundError(f"File {resume_from} not found.")
        self.new_columns = new_columns

        # rate limiting parameters
        self.requests_per_minute = requests_per_minute
        self.request_times = deque()
        self.period = MINUTE

        # parallel inference parameters
        self.max_concurrent = max_concurrent
        self.chat_mode = chat_mode
        self.model.chat_mode = self.chat_mode

    @classmethod
    def from_config(cls, config):
        return cls(
            config.model_config,
            config.data_loader_config,
            config.output_dir,
            resume_from=config.resume_from,
            new_columns=config.new_columns,
            requests_per_minute=config.requests_per_minute,
            max_concurrent=config.max_concurrent,
            chat_mode=config.chat_mode,
        )

    def fetch_previous_inference_results(self):
        """This method loads the contents of the resume_from file and validates if it
        contains the required columns and keys in alignment with the current model configuration."""

        logging.info(f"Resuming inference from {self.resume_from}")
        # fetch previous results from the provided resume_from file.
        pre_inf_results_df = DataReader(self.resume_from, format=".jsonl").load_dataset()

        # add new columns listed by the user to the previous inference results, in case we know that the current model will
        # generate new columns that were not present in the previous results
        if self.new_columns:
            for col in self.new_columns:
                if col not in pre_inf_results_df.columns:
                    pre_inf_results_df[col] = None

        # validate the resume_from contents both stand-alone and against the current model response keys
        with self.data_loader as loader:
            _, sample_model_input, sample_model_kwargs = loader.get_sample_model_input()
            sample_data_keys = loader.reader.read().keys()

            # verify that "model_output" and "is_valid" columns are present
            if "model_output" not in pre_inf_results_df.columns or "is_valid" not in pre_inf_results_df.columns:
                raise ValueError("Columns 'model_output' and 'is_valid' are required in the resume_from file.")

            # perform a sample inference call to get the model output keys and validate the resume_from contents
            sample_response_dict = self.model.generate(*sample_model_input, **sample_model_kwargs)
            if not sample_response_dict["is_valid"]:
                raise ValueError(
                    "Sample inference call for resume_from returned invalid results, please check the model configuration."
                )
            # check if the inference response dictionary contains the same keys as the resume_from file
            eventual_keys = set(sample_response_dict.keys()) | set(sample_data_keys)

            # in case of resuming from a file that was generated by an older version of the model,
            # we let the discrepancy in the reserved keys slide and later set the missing keys to None
            match_keys = set(pre_inf_results_df.columns) | set(INFERENCE_RESERVED_NAMES)

            if set(eventual_keys) != match_keys:
                diff = set(eventual_keys) ^ set(match_keys)
                raise ValueError(
                    f"Columns in resume_from file do not match the current input data and inference response. "
                    f"Problemtaic columns: {diff}"
                )

        # find the last uid that was inferenced
        last_uid = pre_inf_results_df["uid"].astype(int).max()
        logging.info(f"Last uid inferenced: {last_uid}")
        return pre_inf_results_df, last_uid

    def validate_response_dict(self, response_dict):
        # Validate that the response dictionary contains the required fields
        # "model_output" and "is_valid" are mandatory fields to be returned by any model
        if "model_output" not in response_dict or "is_valid" not in response_dict:
            raise ValueError("Response dictionary must contain 'model_output' and 'is_valid' keys.")

    def retrieve_exisiting_result(self, data, pre_inf_results_df):
        """Finds the previous result for the given data point from the pre_inf_results_df and returns it if it is valid
        data: dict, data point to be inferenced
        pre_inf_results_df: pd.DataFrame, previous inference results
        """
        prev_results = pre_inf_results_df[pre_inf_results_df.uid == data["uid"]]
        if prev_results.empty:
            return None
        prev_result_is_valid = bool(prev_results["is_valid"].values[0])
        prev_model_output = prev_results["model_output"].values[0]

        if prev_result_is_valid:
            logging.info(f"Skipping inference for uid: {data['uid']}. Using previous results.")
            try:
                prev_model_tokens = prev_results["n_output_tokens"].values[0]
            except KeyError:
                logging.warn(
                    "Previous results do not contain 'n_output_tokens' column, setting to None for this data point."
                )
                prev_model_tokens = None
            try:
                prev_model_time = prev_results["response_time"].values[0]
            except KeyError:
                logging.warn(
                    "Previous results do not contain 'response_time' column, setting to None for this data point."
                )
                prev_model_time = None

            data["model_output"], data["is_valid"], data["n_output_tokens"], data["response_time"] = (
                prev_model_output,
                prev_result_is_valid,
                prev_model_tokens,
                prev_model_time,
            )
            # add remaining pre_inf_results_df columns to the data point, making sure to update the previous_messages column
            for col in pre_inf_results_df.columns:
                if col not in data or col == "previous_messages":
                    data[col] = prev_results[col].values[0]

            return data

    def run(self):
        if self.max_concurrent > 1:
            asyncio.run(self._run_par())
        else:
            self._run()

    def _run(self):
        """sequential inference"""
        if self.resume_from:
            pre_inf_results_df, last_uid = self.fetch_previous_inference_results()
        with self.data_loader as loader:
            with self.writer as writer:
                for data, model_args, model_kwargs in tqdm(loader, desc="Inference Progress:"):
                    if self.chat_mode and data.get("is_valid", True) is False:
                        continue
                    if self.resume_from and (data["uid"] <= last_uid):
                        prev_result = self.retrieve_exisiting_result(data, pre_inf_results_df)
                        if prev_result:
                            writer.write(prev_result)
                            continue

                    # generate text from model (optionally at a limited rate)
                    if self.requests_per_minute:
                        while len(self.request_times) >= self.requests_per_minute:
                            # remove the oldest request time if it is older than the rate limit period
                            if time.time() - self.request_times[0] > self.period:
                                self.request_times.popleft()
                            else:
                                # rate limit is reached, wait for a second
                                time.sleep(1)
                        self.request_times.append(time.time())
                    response_dict = self.model.generate(*model_args, **model_kwargs)
                    self.validate_response_dict(response_dict)
                    # write results
                    data.update(response_dict)
                    writer.write(data)

    from functools import partial

    async def run_in_excutor(self, model_inputs, executor):
        """Run model.generate in a ThreadPoolExecutor.
        args:
            model_inputs (tuple): args and kwargs to be passed to the model.generate function.
            executor (ThreadPoolExecutor): ThreadPoolExecutor instance.
        """
        loop = asyncio.get_event_loop()

        # function to run in executor with args and kwargs
        def sub_func(model_inputs):
            return self.model.generate(*model_inputs[0], **model_inputs[1])

        return await loop.run_in_executor(executor, sub_func, model_inputs)

    async def _run_par(self):
        """parallel inference"""
        concurrent_inputs = []
        concurrent_metadata = []
        if self.resume_from:
            pre_inf_results_df, last_uid = self.fetch_previous_inference_results()
        with self.data_loader as loader:
            with self.writer as writer:
                for data, model_args, model_kwargs in tqdm(loader, desc="Inference Progress:"):
                    if self.chat_mode and data.get("is_valid", True) is False:
                        continue
                    if self.resume_from and (data["uid"] <= last_uid):
                        prev_result = self.retrieve_exisiting_result(data, pre_inf_results_df)
                        if prev_result:
                            writer.write(prev_result)
                            continue

                    # if batch is ready for concurrent inference
                    elif len(concurrent_inputs) >= self.max_concurrent:
                        with ThreadPoolExecutor() as executor:
                            await self.run_batch(concurrent_inputs, concurrent_metadata, writer, executor)
                        concurrent_inputs = []
                        concurrent_metadata = []
                    # add data to batch for concurrent inference
                    concurrent_inputs.append((model_args, model_kwargs))
                    concurrent_metadata.append(data)
                # if data loader is exhausted but there are remaining data points that did not form a full batch
                if concurrent_inputs:
                    with ThreadPoolExecutor() as executor:
                        await self.run_batch(concurrent_inputs, concurrent_metadata, writer, executor)

    async def run_batch(self, concurrent_inputs, concurrent_metadata, writer, executor):
        """Run a batch of inferences concurrently using ThreadPoolExecutor.
        args:
            concurrent_inputs (list): list of inputs to the model.generate function.
            concurrent_metadata (list): list of metadata corresponding to the inputs.
            writer (JsonLinesWriter): JsonLinesWriter instance to write the results.
            executor (ThreadPoolExecutor): ThreadPoolExecutor instance.
        """
        tasks = [asyncio.create_task(self.run_in_excutor(input_data, executor)) for input_data in concurrent_inputs]
        results = await asyncio.gather(*tasks)
        for i in range(len(concurrent_inputs)):
            data, response_dict = concurrent_metadata[i], results[i]
            self.validate_response_dict(response_dict)
            # prepare results for writing
            data.update(response_dict)
            writer.write(data)
