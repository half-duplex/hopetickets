#!/usr/bin/env python3
# coding=utf-8

import csv
from datetime import datetime
import hashlib
import logging
import os
import smtplib
import sqlite3
import sys
from textwrap import dedent

parent_dir = os.path.abspath(os.path.dirname(__file__))
vendor_dir = os.path.join(parent_dir, "vendor")
sys.path.append(vendor_dir)

import filelock
from tomlkit.toml_file import TOMLFile

logger = logging.getLogger()


class ConTokens:
    _config: dict
    _db: sqlite3.Connection

    def __init__(self, config):
        self._config = config

        self._logger = logging.getLogger(__name__)
        self._logger.setLevel(
            getattr(logging, config.get("log_level", "INFO"), logging.INFO)
        )

        if "db_file" not in config:
            raise Exception("ConfigurationError", "db_file not found in config")
        if "token_types" not in config or not isinstance(config["token_types"], list):
            raise Exception(
                "ConfigurationError", "token_types not found in config or not a list"
            )

        self._db = sqlite3.connect(config["db_file"])
        self._db.isolation_level = "EXCLUSIVE"

    def init_db(self):
        self._logger.info("Creating database")
        self._db.execute(
            """
            create table tokens (
                token char(64) primary key not null,
                token_type varchar(32) not null,
                email varchar(128) default null,
                used_at timestamp default null,
                exported boolean not null default false
            )"""
        )
        self._db.execute("create index idx_email on tokens (token_type, email)")
        self._db.execute("create index idx_exported on tokens (token_type, exported)")

    def gen_tokens(self, token_type, count=500):
        self._logger.info("Generating %s new %s tokens", count, token_type)
        if token_type not in self._config["token_types"]:
            self._logger.error("Invalid token type %s", token_type)
            raise Exception("InvalidTokenType")

        self._db.execute("begin transaction")
        self._db.executemany(
            "insert into tokens (token, token_type) values (?,?)",
            self._make_tokens(count, token_type),
        )
        self._db.commit()

    def _make_tokens(self, count, token_type=None):
        """Generator for new tokens
        If token_type provided, returns a tuple like executemany wants
        Probably a better way, but done > perfect
        """
        if token_type:
            for _ in range(count):
                yield (self._make_token(token_type), token_type)
        else:
            for _ in range(count):
                yield self._make_token(token_type)

    def _make_token(self, token_type=""):
        token_raw = hashlib.sha256(os.urandom(256)).hexdigest()
        prefix = self._config.get("token_prefix", "")
        if len(prefix) > 0:
            prefix += token_type[0].upper() + "-"
            return prefix + token_raw[len(prefix) :]
        else:
            return token_raw

    def _get_tokens(self, token_type, exported, set_exported=True, hashed=False):
        if set_exported and exported:
            raise Exception("Marking exported without exporting unexported is nonsense")
        cur = self._db.cursor()
        cur.execute("begin exclusive")

        cur.execute(
            "select token from tokens where token_type=? and exported=?",
            (token_type, exported),
        )
        for row in cur.fetchall():
            if hashed:
                yield [hashlib.sha256(row[0].encode("ascii")).hexdigest(), "unused"]
            else:
                yield row

        if set_exported:
            cur.execute(
                "update tokens set exported=true where token_type=? and exported=false",
                (token_type,),
            )
            self._logger.info(
                "Marked %s %s tokens as exported", cur.rowcount, token_type
            )

        self._db.commit()

    def export(self, token_type, filename, exported=False):
        self._logger.info("Exporting %s tokens to %s", token_type, filename)
        if token_type not in self._config["token_types"]:
            self._logger.error("Invalid token type %s", token_type)
            raise Exception("InvalidTokenType")
        with open(filename, "w") as f:
            csv_writer = csv.writer(f)
            csv_writer.writerows(
                self._get_tokens(token_type, exported, set_exported=False)
            )
            f.flush()
            os.fsync(f.fileno())
        filename = filename.rsplit(".", 1)[0] + "-hashed.csv"
        with open(filename, "w") as f:
            csv_writer = csv.writer(f)
            csv_writer.writerows(self._get_tokens(token_type, exported, hashed=True))
            f.flush()
            os.fsync(f.fileno())

    def _import_csv_sets(self, token_type, filename, exported=True):
        with open(filename, "r") as f:
            csv_reader = csv.reader(f)
            for row in csv_reader:
                if len(row) < 2:
                    raise Exception("InvalidSentRecord", "Missing fields: {}".format(repr(row)))
                email, token = row[:2]
                if len(token) != self._config.get("token_length", 64):
                    self._logger.warning("Bad token length of %s on record %r", len(token), row)
                yield token, token_type, email, exported

    def importcsv(self, token_type, filename, exported=True):
        self._logger.info("Importing %s tokens from %s as %s", token_type, filename, "exported" if exported else "not exported")
        if token_type not in self._config["token_types"]:
            self._logger.error("Invalid token type %s", token_type)
            raise Exception("InvalidTokenType")

        self._db.execute("begin exclusive")
        self._db.executemany(
            "insert into tokens (token, token_type, email, exported) values (?,?,?,?)",
            self._import_csv_sets(token_type, filename, exported),
        )
        self._db.commit()

        self._logger.info("Imported %s tokens", self._db.total_changes)

    def stats(self):
        cur = self._db.cursor()
        cur.execute(
            "select token_type, email is not null as mailed, exported, count(*) from tokens "
            "group by token_type, mailed, exported"
        )
        res = cur.fetchall()
        ret = {}
        for row in res:
            ret[
                "{} {}{}".format(
                    row[0],
                    "sent" if row[1] else "available",
                    "" if row[2] else " unexported",
                )
            ] = row[3]
        return ret

    def check_available(self, token_type):
        if token_type not in self._config["token_types"]:
            self._logger.error("Invalid token type %s", token_type)
            raise Exception("InvalidTokenType")

        cur = self._db.cursor()
        cur.execute(
            "select count(*) from tokens where token_type=? and email is null",
            (token_type,),
        )
        return cur.fetchone()[0]

    def take_tokens(self, token_type, address, count=1):
        if token_type not in self._config["token_types"]:
            self._logger.error("Invalid token type %s", token_type)
            raise Exception("InvalidTokenType")

        # if not enough, make another batch. a little early for concurrency
        while self.check_available(token_type) < count + 20:
            self.gen_tokens(token_type)

        cur = self._db.cursor()
        cur.execute("begin exclusive")

        # get tokens
        cur.execute(
            "select token from tokens where token_type=? and email is null limit ?",
            (token_type, count),
        )
        tokens = [x[0] for x in cur.fetchall()]

        # set email/used
        now = datetime.now()
        for token in tokens:
            cur.execute(
                "update tokens set email=?, used_at=? where token=?",
                (address, now, token),
            )

        self._logger.info(
            "Issued %s %s tokens to %s: %s",
            count,
            token_type,
            address,
            ",".join(tokens),
        )

        cur.execute("commit")
        return tokens

    def find_tokens(self, token_type, address):
        if token_type not in self._config["token_types"]:
            self._logger.error("Invalid token type %s", token_type)
            raise Exception("InvalidTokenType")

        cur = self._db.cursor()
        cur.execute(
            "select token from tokens where token_type=? and email=?",
            (token_type, address),
        )
        tokens = [x[0] for x in cur.fetchall()]
        return tokens


class ConEmails:
    def __init__(self, config):
        self._config = config

        self._logger = logging.getLogger(__name__)
        self._logger.setLevel(
            getattr(logging, config.get("log_level", "INFO"), logging.INFO)
        )
        for key in ["sender_name", "sender_email", "subject", "messages"]:
            if key not in config:
                raise Exception("ConfigurationError", key + " not found in config")
        if not isinstance(config["messages"], dict):
            raise Exception("ConfigurationError", "'messages' must be a dict")

    def send(self, token_type, address, tokens):
        self._logger.info(
            "Sending %s %s tokens to %s", len(tokens), token_type, address
        )

        if token_type not in self._config["messages"]:
            raise Exception("Message not found for " + token_type)
        if len(tokens) == 0:
            raise Exception("Refusing to send no tokens")

        # Build ticket code section
        if len(tokens) == 1:
            str_codesare = "code is"
            str_tickets = "ticket"
        else:
            str_codesare = "codes are"
            str_tickets = "tickets"
        token_section = "You purchased {} {}.\n\n".format(len(tokens), str_tickets)
        token_section += "Your ticket {}:\n\n".format(str_codesare)
        token_section += "\n\n".join(tokens)
        token_section += "\n"

        # Build message
        message = "From: {} <{}>\n".format(
            self._config["sender_name"], self._config["sender_email"]
        )
        message += "To: <{}>\n".format(address)
        message += "Subject: {}\n".format(
            self._config["subject"].format(str_tickets=str_tickets)
        )
        message += "\n"

        message += self._config["messages"][token_type].format(tickets=token_section)

        try:
            smtp = smtplib.SMTP(self._config.get("smtp_host", "localhost"))
            smtp.sendmail(self._config["sender_email"], address, message)
            self._logger.info("Sent message to %s", address)
        except smtplib.SMTPException as e:
            self._logger.info("Error sending message to %s", address)
            raise e


if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)-12s %(levelname)-8s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    config_file = os.environ.get(
        "CONFIG", os.path.join(os.path.dirname(__file__), "config.toml")
    )
    toml = TOMLFile(config_file)
    config = toml.read()

    if len(sys.argv) == 1 or sys.argv[1] == "help":
        print(
            dedent(
                """
        Usage: {} COMMAND
          Where COMMAND is one of:
            help - Show this
            init - Create database
            gentokens TYPE X - Generate X more tokens of TYPE
            import TYPE FILENAME - Import CSV of TYPE tokens (imported as EXPORTED and USED!)
            export TYPE [FILENAME] - Generate CSV of all un-exported TYPE tokens
            issue TYPE EMAIL [COUNT] - Take tokens, print to console, mark used
            find TYPE EMAIL - Find previously issued tokens
            send TYPE EMAIL X - Send X unused tokens of TYPE to EMAIL
            sendcsv TYPE FILE - Send unused tokens of TYPE to each EMAIL,COUNT from FILE
            resend TYPE EMAIL - Resend all previously issued tokens of TYPE for EMAIL
            stats - Print statistics about tokens
        """.format(
                    sys.argv[0]
                )
            )
        )
        exit(4)

    lock = filelock.FileLock("database.lock", timeout=30)
    tokens = ConTokens(config["tokens"])
    emails = ConEmails(config["emails"])

    if sys.argv[1] == "init":
        with lock:
            tokens.init_db()
    elif sys.argv[1] == "gentokens":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE COUNT".format(sys.argv[0], sys.argv[1]))
            exit(4)
        token_type = sys.argv[2]
        count = int(sys.argv[3])

        with lock:
            tokens.gen_tokens(token_type, count)
    elif sys.argv[1] == "import":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE FILENAME".format(sys.argv[0], sys.argv[1]))
            exit(4)
        with lock:
            tokens.importcsv(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "export":
        if len(sys.argv) < 3:
            print("Usage: {} {} TYPE [FILENAME]".format(sys.argv[0], sys.argv[1]))
            exit(4)

        if len(sys.argv) > 3:
            filename = sys.argv[3]
        else:
            now = datetime.now().strftime("%Y%m%d-%H%M%S.%f")
            filename = "export-{}-{}.csv".format(sys.argv[2], now)
        with lock:
            tokens.export(sys.argv[2], filename)
    elif sys.argv[1] == "issue":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE EMAIL [COUNT]".format(sys.argv[0], sys.argv[1]))
            exit(4)
        if len(sys.argv) > 4:
            count = int(sys.argv[4])
        else:
            count = 1
        print("Issued {} {} tokens for {}:".format(count, sys.argv[2], sys.argv[3]))
        with lock:
            print(*tokens.take_tokens(sys.argv[2], sys.argv[3], count), sep="\n")
    elif sys.argv[1] == "find":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE EMAIL".format(sys.argv[0], sys.argv[1]))
            exit(4)
        with lock:
            issued_tokens = tokens.find_tokens(sys.argv[2], sys.argv[3])
        print(
            "Found {} {} tokens for {}:".format(
                len(issued_tokens), sys.argv[2], sys.argv[3]
            )
        )
        print(*issued_tokens, sep="\n")
    elif sys.argv[1] == "send":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE EMAIL [COUNT]".format(sys.argv[0], sys.argv[1]))
            exit(4)
        if len(sys.argv) > 4:
            count = int(sys.argv[4])
        else:
            count = 1
        print("Sending {} {} tokens to {}".format(count, sys.argv[2], sys.argv[3]))
        with lock:
            issued_tokens = tokens.take_tokens(sys.argv[2], sys.argv[3], count)
        emails.send(sys.argv[2], sys.argv[3], issued_tokens)
    elif sys.argv[1] == "sendcsv":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE FILE".format(sys.argv[0], sys.argv[1]))
            exit(4)
        print("Sending {} tokens for {}".format(sys.argv[2], sys.argv[3]))
        with lock:
            with open(sys.argv[3], "r") as f:
                csv_reader = csv.reader(f)
                for row in csv_reader:
                    email = row[0]
                    count = int(row[1])
                    print(
                        "Sending {} {} tokens to {}".format(count, sys.argv[2], email)
                    )
                    issued_tokens = tokens.take_tokens(sys.argv[2], email, count)
                    emails.send(sys.argv[2], email, issued_tokens)
    elif sys.argv[1] == "resend":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE EMAIL".format(sys.argv[0], sys.argv[1]))
            exit(4)
        with lock:
            issued_tokens = tokens.find_tokens(sys.argv[2], sys.argv[3])
        print(
            "Resending {} {} tokens to {}".format(
                len(issued_tokens), sys.argv[2], sys.argv[3]
            )
        )
        emails.send(sys.argv[2], sys.argv[3], issued_tokens)
    elif sys.argv[1] == "stats":
        with lock:
            for token_type, count in tokens.stats().items():
                print("{}:\t{}".format(token_type, count))
    elif sys.argv[1] == "db_exec":
        with lock:
            tokens._db.execute(sys.argv[2])
            tokens._db.commit()
        print(tokens._db.total_changes)
    else:
        logger.error(
            "Unknown command %s. Run %s help for help.", sys.argv[1], sys.argv[0]
        )
