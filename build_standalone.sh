python -m venv venv
source venv/bin/activate
python -m pip install -U pip wheel setuptools setuptools_scm pyinstaller
python -m pip install .
python tools/build_standalone.py
source venv/bin/activate
deactivate
rm -rf venv
