from setuptools import setup, find_packages

setup(
    name="cv-crypto",
    version="0.1.0",
    description="Arkive shared crypto-agile hybrid cryptography layer",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "cryptography>=42.0.0",
        "argon2-cffi>=23.1.0",
    ],
    extras_require={
        # Post-quantum primitives via liboqs. Optional so the prototype runs
        # without native deps; install for real quantum-safe operation. NOTE: the
        # PyPI package literally named `oqs` is a DIFFERENT, unrelated project —
        # the Open Quantum Safe binding is `liboqs-python` (also imports as `oqs`)
        # and needs the native liboqs library.
        "pq": ["liboqs-python>=0.10.0"],
    },
)
