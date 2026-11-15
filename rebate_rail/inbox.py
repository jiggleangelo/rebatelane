from dataclasses import dataclass
import time

@dataclass
class InboxItem:
    id: str
    kind: str
    intent_id: str
    account_id: str
    opened_at: int
    status: str = "OPEN"
    resolution: str | None = None

class Inbox:
    def __init__(self):
        self.items = {}
        self._next = 1

    def open(self, kind, intent_id, account_id):
        item = InboxItem(f"ix_{self._next}", kind, intent_id, account_id, int(time.time()))
        self._next += 1
        self.items[item.id] = item
        return item

    def resolve(self, item_id, resolution):
        item = self.items[item_id]
        item.status, item.resolution = "RESOLVED", resolution
        return item

    def open_for(self, intent_id):
        return [i for i in self.items.values() if i.intent_id == intent_id and i.status == "OPEN"]
