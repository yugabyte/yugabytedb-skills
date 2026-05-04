# Python client for the YBA API

A complete, copy-paste-ready Python wrapper. Designed to be small enough to drop into a script or notebook without `pip install`-ing anything except `requests` (always required) and `urllib3` (transitive, used to silence warnings on self-signed certs).

The intent is to keep boilerplate out of subsequent calls so an AI assistant can compose multi-step API workflows in a few lines per step.

## Full wrapper

```python
"""yba.py — minimal helper for the YugabyteDB Anywhere REST API."""
import time
import requests
import urllib3

_CUSTOMER_ID_CACHE: dict[str, str] = {}


class YbaApiError(RuntimeError):
    pass


def yba_request(
    base_url: str,
    endpoint: str,
    token: str,
    *,
    customer_id: str | None = None,
    method: str = "GET",
    payload: dict | list | None = None,
    params: dict | None = None,
    verify: bool = True,
    timeout: int = 30,
    wait: bool = False,
    wait_timeout: int = 600,
    poll_interval: float = 2.0,
) -> dict | list:
    """Invoke a YBA REST API endpoint.

    If `wait` is True and the response contains a `taskUUID`, poll
    `/customers/{customer_id}/tasks/{taskUUID}` until terminal state.
    """
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-AUTH-YW-API-TOKEN": token,
    }
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"

    r = requests.request(
        method.upper(), url,
        headers=headers, json=payload, params=params,
        timeout=timeout, verify=verify,
    )
    if not r.ok:
        try:
            err = r.json().get("error", r.text)
        except ValueError:
            err = r.text
        raise YbaApiError(f"{method} {url} → {r.status_code}: {err}")
    data = r.json()

    if not wait or not isinstance(data, dict) or "taskUUID" not in data or not customer_id:
        return data

    task_url = (
        f"{base_url.rstrip('/')}/api/v1/customers/{customer_id}"
        f"/tasks/{data['taskUUID']}"
    )
    deadline = time.time() + wait_timeout
    task = data
    while time.time() < deadline:
        task = requests.get(task_url, headers=headers, timeout=10, verify=verify).json()
        if task.get("percent") == 100 or task.get("status") in ("Aborted", "Failure"):
            return task
        time.sleep(poll_interval)
    return task  # final task state on timeout


def yba_login(base_url: str, email: str, password: str, *, verify: bool = True) -> dict:
    """POST /api/v1/api_login. Returns {apiToken, customerUUID, userUUID, ...}.

    Note: rotates any previously issued token for this user.
    """
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    r = requests.post(
        f"{base_url.rstrip('/')}/api/v1/api_login",
        json={"email": email, "password": password},
        timeout=30, verify=verify,
    )
    r.raise_for_status()
    return r.json()


def yba_register(base_url: str, code: str, name: str, email: str, password: str,
                 *, verify: bool = True) -> dict:
    """POST /api/v1/register?generateApiToken=1. First-time setup only.

    On a previously registered instance YBA returns 'cannot register multiple
    accounts' — fall back to yba_login() in that case.
    """
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    r = requests.post(
        f"{base_url.rstrip('/')}/api/v1/register",
        params={"generateApiToken": 1},
        json={"code": code, "name": name, "email": email, "password": password},
        timeout=30, verify=verify,
    )
    if r.status_code == 400 and "cannot register multiple accounts" in r.text:
        return yba_login(base_url, email, password, verify=verify)
    r.raise_for_status()
    return r.json()


def yba_customer_id(base_url: str, token: str, *, verify: bool = True,
                    refresh: bool = False) -> str:
    """Return the (cached) customer UUID for a single-tenant YBA instance."""
    if not refresh and base_url in _CUSTOMER_ID_CACHE:
        return _CUSTOMER_ID_CACHE[base_url]
    customers = yba_request(base_url, "/api/v1/customers", token, verify=verify)
    cid = customers[0]["uuid"]
    _CUSTOMER_ID_CACHE[base_url] = cid
    return cid
```

That's the entire client. Less than 100 lines.

## Idiomatic usage

Splat the connection args so each call is a single line of intent:

```python
import os
from yba import yba_request, yba_login, yba_customer_id

login = yba_login(os.environ["YBA_URL"], os.environ["YBA_EMAIL"],
                  os.environ["YBA_PASSWORD"], verify=False)

yba = {
    "base_url": os.environ["YBA_URL"],
    "token": login["apiToken"],
    "customer_id": login["customerUUID"],
    "verify": False,
}

# Read
universes = yba_request(**yba, endpoint=f"/api/v1/customers/{yba['customer_id']}/universes")
providers = yba_request(**yba, endpoint=f"/api/v1/customers/{yba['customer_id']}/providers")

# Write + wait for the task
backup = yba_request(
    **yba,
    method="POST",
    endpoint=f"/api/v1/customers/{yba['customer_id']}/tables/backup",
    payload={"universeUUID": universes[0]["universeUUID"], "keyspace": "yugabyte",
             "backupType": "PGSQL_TABLE_TYPE", "storageConfigUUID": "<storage-uuid>"},
    wait=True,
)
```

## Resolving a universe by name (avoid client-side filtering)

```python
uuid = yba_request(
    **yba,
    endpoint=f"/api/v1/customers/{yba['customer_id']}/universes/find",
    params={"name": "my-universe"},
)
universe = yba_request(
    **yba,
    endpoint=f"/api/v1/customers/{yba['customer_id']}/universes/{uuid}",
)
```

## Picking v1 vs v2

Both versions live behind the same wrapper — only the path changes. Examples:

```python
# v1: list universes
yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/universes")

# v2: create universe (smaller, hand-curated payload — see openapi spec)
yba_request(**yba, method="POST",
            endpoint=f"/api/v2/customers/{cid}/universes",
            payload=v2_universe_payload, wait=True)
```

If a v2 endpoint exists for what you're doing, prefer it: the payload is typically a few-times smaller and the field names are stable across releases.

## Error handling pattern

`yba_request` raises `YbaApiError` with the YBA-supplied `error` string included. Catch it where retries make sense (e.g. transient 5xx during a YBA restart):

```python
from yba import YbaApiError

try:
    yba_request(**yba, endpoint=...)
except YbaApiError as e:
    if "Connection reset" in str(e):
        ...  # retry
    else:
        raise
```

## What this client deliberately does NOT do

- **No retries.** Add them at the call site if you need them — the failure modes differ per endpoint.
- **No JSON-template engine.** Build payloads as plain dicts; if you need parameterised payloads, `string.Template` or `pathlib.Path(...).read_text() % vars` is enough.
- **No parallel task waiting.** If you fire off N tasks and need to wait on all of them, call the wrapper with `wait=False`, collect the `taskUUID`s, then poll them with your own `concurrent.futures` or `asyncio` loop.
- **No pagination wrapper.** Most YBA list endpoints return everything in one response. The few that paginate (audit logs, alerts) document their own params — handle them at the call site.
