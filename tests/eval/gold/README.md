# Text-to-SQL Eval Gold Sets

JSONL files in this directory use the strict versioned case schema and contain
only cases with a complete `review.status: reviewed` record. Generated
candidates from history stay outside this directory and are never release gold
until they are converted to the current schema and independently reviewed.
