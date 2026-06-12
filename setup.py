from setuptools import setup, find_packages

setup(
    name="code-review-agent",
    version="3.2.0",
    packages=find_packages(),
    install_requires=[
        "anthropic>=0.39.0",
        "openai>=1.0.0",
        "chromadb>=0.5.0",
        "rich>=13.0.0",
        "python-dotenv>=1.0.0",
        "fastapi>=0.115.0",
        "uvicorn>=0.30.0",
    ],
    entry_points={
        "console_scripts": [
            "code-review=src.cli:main",
        ],
    },
    python_requires=">=3.10",
)
