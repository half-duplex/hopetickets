# hopetickets


## Installing
```sh
git clone https://github.com/half-duplex/hopetickets.git
cd hopetickets
git submodule init
git submodule update
```


## Running
Thoretically, just run `./main.py`

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


## Using
```sh
./main.py init
./main.py gentokens attendee 1000
# repeat for other types
./main.py export attendee
# repeat for other types. produces export-*.csv
./main.py send attendee you@example.com 2
# to send from CSV (rows of email,count)
./main.py sendcsv attendee attendees.csv
```
Note that more tokens will be automatically generated if unused ones run out.
See `./main.py help` for other commands, like statistics and resending codes.

To export the entire database as CSV, use the `sqlite3` command:
```sh
sqlite3 -csv -header tokens.db 'select * from tokens' >dump.csv
```
Note that importing this CSV may not preserve the difference between NULL
(empty column in CSV, token not used) and an empty string (empty quotes column
in CSV, token used with no email).


## Contributing
Install as above, but add the development requirements:
```sh
virtualenv -p python3 venv
. venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```
Before committing, format and lint:
```sh
black . && flake8
```
