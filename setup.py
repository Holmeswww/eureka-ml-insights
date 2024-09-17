from setuptools import setup, find_packages


setup(
    name='eureka_ml_insights',
    version='0.1.0',
    author='Microsoft Research',
    author_email='eureka-ml-insights@microsoft.com',
    description='Eureka ML Insights Framework',
    long_description=open('README.md', encoding="utf-8").read(),
    long_description_content_type='text/markdown',
    url='https://github.com/microsoft/eureka-ml-insights',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'anthropic>=0.30.0',
        'azure-ai-textanalytics>=5.3.0',
        'azure-core>=1.29.5',
        'azure-keyvault-secrets>=4.8.0',
        'azure-identity>=1.16.0',
        'azure-storage-blob>=12.20.0',
        'datasets>=2.19.0',
        'fuzzywuzzy>=0.18.0',
        'jsonlines>=2.0.0',
        'pandas>=2.2.1',
        'pillow>=10.0.1',
        'torch==2.1.2',
        'numpy==1.26.4',
        'tqdm>=4.65.0',
        'jinja2>=3.1.3',
        'transformers>=4.40.2',
        'immutabledict>=4.2.0',
        'langdetect>=1.0.9',
        'nltk>=3.9.1',
        'absl-py>=2.1.0',
        'python-levenshtein>=0.12.2',
        'google-generativeai>=0.7.0',
        'openai>=1.35.5',
        'bitsandbytes>=0.42.0',
        'accelerate>=0.21.0',
        'pycocotools>=2.0.8',
    ],
    classifiers=[
        # Full list at https://pypi.org/classifiers/
        'Programming Language :: Python :: 3',
        'License :: Apache License 2.0',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.8',
)
