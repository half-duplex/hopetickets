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
                yield (self._make_token(), token_type)
        else:
            for _ in range(count):
                yield self._make_token()

    def _make_token(self):
        return hashlib.sha256(os.urandom(256)).hexdigest()

    def _get_tokens(self, token_type, exported, set_exported=True):
        if set_exported and exported:
            raise Exception("Marking exported without exporting unexported is nonsense")
        cur = self._db.cursor()
        cur.execute("begin exclusive")
        cur.execute(
            "select token from tokens where token_type=? and exported=?",
            (token_type, exported),
        )
        for row in cur.fetchall():
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
        with open(filename, "w") as f:
            csv_writer = csv.writer(f)
            csv_writer.writerows(self._get_tokens(token_type, exported))
            f.flush()
            os.fsync(f.fileno())

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
        cur = self._db.cursor()
        cur.execute(
            "select count(*) from tokens where token_type=? and email is null",
            (token_type,),
        )
        return cur.fetchone()[0]

    def take_tokens(self, token_type, address, count=1):
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

    tokens = ConTokens(config["tokens"])
    emails = ConEmails(config["emails"])

    if len(sys.argv) == 1 or sys.argv[1] == "help":
        print(
            dedent(
                """
        Usage: {} COMMAND
          Where COMMAND is one of:
            help - Show this
            init - Create database
            gentokens TYPE X - Generate X more tokens of TYPE
            export TYPE [FILENAME] - Generate CSV of all un-exported TYPE tokens
            issue TYPE EMAIL [COUNT] - Take tokens, print to console, mark used
            find TYPE EMAIL - Find previously issued tokens
            send TYPE EMAIL X - Send X unused tokens of TYPE to EMAIL
            resend TYPE EMAIL - Resend all previously issued tokens of TYPE for EMAIL
            stats - Print statistics about tokens
        """.format(
                    sys.argv[0]
                )
            )
        )
        exit(4)

    if sys.argv[1] == "init":
        tokens.init_db()
    elif sys.argv[1] == "gentokens":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE COUNT".format(sys.argv[0], sys.argv[1]))
            exit(4)
        token_type = sys.argv[2]
        count = int(sys.argv[3])

        tokens.gen_tokens(token_type, count)
    elif sys.argv[1] == "export":
        if len(sys.argv) < 3:
            print("Usage: {} {} TYPE [FILENAME]".format(sys.argv[0], sys.argv[1]))
            exit(4)

        if len(sys.argv) > 3:
            filename = sys.argv[3]
        else:
            now = datetime.now().strftime("%Y%m%d-%H%M%S.%f")
            filename = "export-{}-{}.csv".format(sys.argv[2], now)
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
        print(*tokens.take_tokens(sys.argv[2], sys.argv[3], count), sep="\n")
    elif sys.argv[1] == "find":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE EMAIL".format(sys.argv[0], sys.argv[1]))
            exit(4)
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
        issued_tokens = tokens.take_tokens(sys.argv[2], sys.argv[3], count)
        emails.send(sys.argv[2], sys.argv[3], issued_tokens)
    elif sys.argv[1] == "resend":
        if len(sys.argv) < 4:
            print("Usage: {} {} TYPE EMAIL".format(sys.argv[0], sys.argv[1]))
            exit(4)
        issued_tokens = tokens.find_tokens(sys.argv[2], sys.argv[3])
        print("Resending {} {} tokens to {}".format(len(issued_tokens), sys.argv[2], sys.argv[3]))
        emails.send(sys.argv[2], sys.argv[3], issued_tokens)
    elif sys.argv[1] == "stats":
        for token_type, count in tokens.stats().items():
            print("{}:\t{}".format(token_type, count))
    elif sys.argv[1] == "db_exec":
        tokens._db.execute(sys.argv[2])
        tokens._db.commit()
        print(tokens._db.total_changes)
    else:
        logger.error(
            "Unknown command %s. Run %s help for help.", sys.argv[1], sys.argv[0]
        )
