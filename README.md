# hopetickets

## Installing
```sh
git clone https://github.com/half-duplex/hopetickets.git
cd hopetickets
git submodule init
```

## Contributing
Install as above, but add the development requirements:
```sh
virtualenv -p python3 venv
. venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```
Before committing, use `black` and `flake8`:
  black . && flake8 .


## Running
Thoretically, just run ./main.py

If that doesn't work, install with the Contributing instructions (dev
requirements optional) and:

```sh
. venv/bin/activate
./main.py [...]
```

OR

```
venv/bin/python main.py [...]
```
