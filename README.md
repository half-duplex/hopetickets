# hopetickets

## Installing
```sh
git clone https://github.com/half-duplex/hopetickets.git
cd hopetickets
git submodule init
```

## Using
```sh
./main.py init
./main.py gentokens attendee 1000
# repeat for other types
./main.py export attendee
# repeat for other types. produces export-*.csv
./main.py send attendee you@example.com 2
```
Note that more tokens will be automatically generated if unused ones run out.
See `./main.py help` for other commands, like statistics and resending codes.

## Contributing
Install as above, but add the development requirements:
```sh
virtualenv -p python3 venv
. venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```
Before committing, format and lint:
```sh
black main.py && flake8 main.py
```


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
