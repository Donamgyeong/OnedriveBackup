"""Bounded size-aware scheduling over a temporary, indexed SQLite snapshot."""
from collections import Counter, deque


def size_class(size):
    return 0 if size < 1024 ** 2 else (1 if size < 64 * 1024 ** 2 else 2)


class DownloadQueue:
    weights = (2, 2, 4)

    def __init__(self, db):
        self.db = db
        self.buffers = [deque(), deque(), deque()]
        self.cursors = [0, 0, 0]
        self.exhausted = [False, False, False]
        self.selected = 0

    def pop(self, active):
        for category in range(3):
            if not self.buffers[category] and not self.exhausted[category]:
                rows = self.db.all("""SELECT j.*,q.size,q.category FROM download_schedule q
                    JOIN jobs j ON j.job_id=q.job_id WHERE q.category=? AND q.job_id>?
                    ORDER BY q.job_id LIMIT 256""", (category, self.cursors[category]))
                self.buffers[category].extend(dict(row) for row in rows)
                if rows:
                    self.cursors[category] = rows[-1]["job_id"]
                self.exhausted[category] = len(rows) < 256
        available = [i for i in range(3) if self.buffers[i]]
        if not available:
            return None
        counts = Counter(job["category"] for job in active)
        # Every 32nd admission takes the oldest buffered job to prevent starvation.
        oldest = self.selected % 32 == 31
        if oldest:
            category = min(available, key=lambda i: self.buffers[i][0]["job_id"])
        else:
            category = min(available, key=lambda i: ((counts[i] + 1) / self.weights[i], -i))
        drives = Counter(job["drive_id"] for job in active)
        buffer = self.buffers[category]
        # Prefer the least occupied drive within this bounded lookahead. This is
        # fairness, not independent service budgets: all share one governor.
        index = 0 if oldest or not drives else min(range(len(buffer)), key=lambda i: drives[buffer[i]["drive_id"]])
        job = buffer[index]
        del buffer[index]
        self.selected += 1
        return job
