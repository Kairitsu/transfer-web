# Transfer Web

Web interface for transferring files between the local server and a remote server over SSH and rsync.

## Requirements

- Python 3.10+
- `rsync`
- SSH access to the remote server
- A private key available at the path configured in `app.py`

## Run

```bash
python3 app.py
```

The service listens on `127.0.0.1:8756`. In production it is intended to run behind an authenticated reverse proxy.

## Runtime data

Task state and transfer logs are stored outside this repository under:

- `/var/lib/transfer-web`
- `/var/log/transfer-web`

Do not commit SSH private keys, reverse-proxy credentials, state files, or logs.
