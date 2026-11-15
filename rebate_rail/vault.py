import secrets

class Vault:
    def __init__(self):
        self._contacts = {}
        self._outbox = []

    def put(self, contact):
        token = "vlt_" + secrets.token_hex(12)
        self._contacts[token] = contact
        return token

    def send(self, token, message):
        if token not in self._contacts:
            raise KeyError("unknown vault token")
        self._outbox.append((token, message))

    @property
    def sent_count(self):
        return len(self._outbox)
