import base64
import os
import inspect
import pprint
import json
import logging
import signal
import subprocess
import time
# pylint: disable=E0611,E0401
from urllib.parse import urlparse
#pylint: disable=E0401
import records  # https://github.com/kennethreitz/records
import psutil
import requests
import standardpaths
standardpaths.configure(application_name='bitwarden',
                        organization_name='birl.org')

log = logging.getLogger(__name__)
log.propagate = True


class UnimplementedError(Exception):
    pass


def badOrMissingDB(url):
    """run when DB is either missing or not setup properly"""
    print("You need to run liquibase via tools/lb.sh")
    raise IOError("DB %s does not exist: You need to run tools/lb.sh" % url)


def isexception(obj):
    """Given an object, return a boolean indicating whether it is an instance
    or subclass of :py:class:`Exception`.
    stolen from: https://github.com/kennethreitz/records/blob/master/records.py
    """
    if isinstance(obj, Exception):
        return True
    if inspect.isclass(obj) and issubclass(obj, Exception):
        return True
    return False


def connect(dbURL=None):
    """connect to DB and return records.db instance"""
    parsedURL = urlparse(dbURL)
    if parsedURL.scheme != 'sqlite':
        raise UnimplementedError(
            "DB scheme:{} is not currently supported, patches welcome.".format(
                parsedURL.scheme))
    if not os.path.exists(parsedURL.path):
        badOrMissingDB(dbURL)
    # nobody can play with this file, except us, we don't play well with others.
    os.chmod(parsedURL.path, 0o0600)
    db = records.Database(dbURL)
    if 'config' not in db.get_table_names():
        badOrMissingDB(dbURL)
    return db


class Config():
    def __init__(self, db):
        self.db = db

    def set(self, key, value):
        """set the key to equal value in the DB"""
        return self.db.query(
            "INSERT OR REPLACE INTO config (key, value) VALUES (:key, :value)",
            key=key,
            value=value)

    def one(self, rows, default=None, as_dict=False, as_ordereddict=False):
        """implement one from records trunk since it is not released yet"""
        try:
            record = rows[0]
        except IndexError:
            if isexception(default):
                #pylint: disable=E0702
                raise default
            return default
        try:
            rows[1]
        except IndexError:
            pass
        else:
            raise ValueError('RecordCollection contained more than one row. '
                             'Expects only one row when using '
                             'RecordCollection.one')
        if as_dict:
            return record.as_dict()
        elif as_ordereddict:
            return record.as_dict(ordered=True)
        else:
            return record

    def scalar(self, one, default=None):
        """return single column from single row or default"""
        row = self.one(one)
        return row[0] if row else default

    def get(self, key, default=None):
        """return value from DB or default if not set"""
        row = self.db.query("select value from config where key=:key", key=key)
        return self.scalar(row, default)

    @property
    def identurl(self):
        """bitwarden URL"""
        return self.get('ident_url', 'https://identity.bitwarden.com')

    @identurl.setter
    def identurl(self, value):
        return self.set('ident_url', value)

    @property
    def url(self):
        """bitwarden URL"""
        return self.get('url', 'https://api.bitwarden.com')

    @url.setter
    def url(self, value):
        return self.set('url', value)

    @property
    def debug(self):
        """debug"""
        return self.get('debug', False)

    @debug.setter
    def debug(self, value):
        """debug setter"""
        return self.set('debug', value)

    @property
    def encryption_key(self):
        """This is the encrypted encryption key."""
        return self.get('encryption_key', None)

    @encryption_key.setter
    def encryption_key(self, value):
        return self.set('encryption_key', value)

    @property
    def client_token(self):
        """token from bitwarden server."""
        return json.loads(self.get('client_token', None))

    @client_token.setter
    def client_token(self, value):
        """set token"""
        return self.set('client_token', json.dumps(value))

    @property
    def agent_token(self):
        """token to talk with agent."""
        return self.get('agent_token', None)

    @agent_token.setter
    def agent_token(self, value):
        """set token"""
        return self.set('agent_token', value)

    @property
    def agent_port(self):
        """
        localhost port that the agent listens to, when it's running.
        """
        return int(self.get('agent_port', 6277))

    @agent_port.setter
    def agent_port(self, value):
        """setter for agent_port"""
        return self.set('agent_port', int(value))

    @property
    def master_key(self):
        """
        master key that decrypts information.
        """
        ret = None
        if time.time() > self.client_token['token_expires']:
            raise IOError("Token has expired, please login again.")
        key = requests.post("http://127.0.0.1:{}".format(self.agent_port),
                            json={'key': self.agent_token}).json()
        try:
            ret = base64.b64decode(key['master_key'])
        except IndexError:
            log.error("expected master_key but agent returned:%s", pprint.pformat(ret))
        return ret

    @master_key.setter
    def master_key(self, value):
        """setter for master key -- starts agent"""
        log.debug("setting master_key before b64encode:%s", value)
        key = base64.b64encode(value).decode('utf-8')
        pidFile = os.path.join(standardpaths.get_writable_path(
            'app_local_data'), 'agent.pid')
        if os.path.exists(pidFile):
            # agent already running, not so good for us.
            pid = int(open(pidFile, 'r').read())
            if psutil.pid_exists(pid):
                os.kill(pid, signal.SIGTERM)
            else:
                os.unlink(pidFile)

        agent_token = base64.b64encode(os.urandom(16)).decode('utf-8')
        cmd = ['bitwarden-agent', '127.0.0.1:{}'.format(self.agent_port)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        data = {
            'master_key': key,
            'tiemout': self.client_token['token_expires'],
            'agent_token': agent_token
        }
        out = json.dumps(data) + "\n"
        p.stdin.write(out.encode('utf-8'))
        self.agent_token = agent_token
        return True
