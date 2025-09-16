import os, json, base64, time, urllib.parse, urllib.request
import boto3
from botocore.exceptions import ClientError

secrets = boto3.client("secretsmanager")

_SECRET_CACHE: dict[str, tuple[float, str]] = {}
_SECRET_TTL_SECONDS = float(os.environ.get("API_SECRET_TTL_SECONDS", "300"))
_LOG_LEVEL = (os.environ.get("LOG_LEVEL") or "INFO").upper()
_RETURN_DEBUG = (os.environ.get("RETURN_DEBUG") or "false").lower() == "true"
_MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "240000"))


def _should_log(level: str) -> bool:
    order = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}
    return order.get(level, 20) >= order.get(
        "DEBUG" if _LOG_LEVEL == "DEBUG" else _LOG_LEVEL, 20
    )


def _log(level: str, step: str, message: str, **kwargs):
    if not _should_log(level):
        return
    # Avoid leaking secrets in logs
    if "headers" in kwargs and isinstance(kwargs["headers"], dict):
        redacted = {}
        for k, v in kwargs["headers"].items():
            if k.lower() in {
                "authorization",
                "x-api-key",
                "proxy-authorization",
            }:
                redacted[k] = "***"
            else:
                redacted[k] = v
        kwargs["headers"] = redacted
    record = {
        "level": level,
        "step": step,
        "message": message,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if kwargs:
        record.update(kwargs)
    print(json.dumps(record, ensure_ascii=False))


def _from_attrval(value):
    """
    Convert DynamoDB AttributeValue-shaped data (S/N/M/L) into plain Python types.
    Safe to call on already-plain values; returns input if no conversion applies.
    """
    if isinstance(value, dict):
        if "S" in value:
            return value["S"]
        if "N" in value:
            n = value["N"]
            try:
                return float(n) if "." in str(n) else int(n)
            except Exception:
                return n
        if "M" in value and isinstance(value["M"], dict):
            return {k: _from_attrval(v) for k, v in value["M"].items()}
        if "L" in value and isinstance(value["L"], list):
            return [_from_attrval(v) for v in value["L"]]
        # Fallback: attempt shallow conversion
        return {k: _from_attrval(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_attrval(v) for v in value]
    return value


def _as_number(val, default=None):
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        try:
            return float(val) if "." in val else int(val)
        except Exception:
            return default
    if isinstance(val, dict) and "N" in val:
        return _as_number(val["N"], default)
    return default


def _as_number_list(vals, default_list=None):
    if isinstance(vals, list):
        out = []
        for v in vals:
            n = _as_number(v, None)
            if n is None:
                try:
                    n = int(v)  # last resort string->int
                except Exception:
                    continue
            out.append(int(n))
        return out
    # Try AttributeValue list
    if isinstance(vals, dict) and "L" in vals:
        return _as_number_list(vals["L"], default_list)
    return default_list or []


def _get_secret_value(name: str, json_key: str | None = None) -> str:
    """
    Get the secret value from the Secrets Manager
    name: the name of the secret
    json_key: if the secret is a json object, the key of the secret - {"api_key":"...","other":"..."} and you want just "api_key"
    return: the secret value
    """
    resp = secrets.get_secret_value(SecretId=name)
    val = resp.get("SecretString")
    if not val and "SecretBinary" in resp:
        val = resp["SecretBinary"].decode("utf-8")
    if json_key:
        # In case the secret is store as a json object and not a literal string value
        data = json.loads(val)
        return data[json_key]
    return val


def _get_secret_value_with_retry(
    name: str,
    json_key: str | None = None,
    attempts: int = 3,
    backoff: float = 1.5,
) -> str:
    now = time.time()
    # cached contain each app with the time of cache
    cached = _SECRET_CACHE.get(name)
    if cached and cached[0] > now:
        _log(
            "DEBUG",
            "secret_cache",
            "Using cached secret",
            secret_name=name,
            cached_until=cached[0],
        )
        cached_val = cached[1]
        if json_key:
            try:
                return json.loads(cached_val)[json_key]
            except Exception:
                # if the json key is not valid, we will not use the cached value
                pass
        return cached_val

    last_err = None
    for i in range(1, attempts + 1):
        try:
            _log(
                "DEBUG",
                "secret_fetch",
                "Fetching secret",
                secret_name=name,
                attempt=i,
            )
            val = _get_secret_value(name, json_key=None)
            _SECRET_CACHE[name] = (now + _SECRET_TTL_SECONDS, val)
            if json_key:
                try:
                    return json.loads(val)[json_key]
                except Exception as e:
                    last_err = e
                    raise
            return val
        except Exception as e:
            last_err = e
            _log(
                "ERROR",
                "secret_fetch_error",
                "Failed to fetch secret",
                secret_name=name,
                attempt=i,
                error=str(e),
            )
            if i < attempts:
                time.sleep(backoff**i)
    raise last_err


def _resolve_api_key(event_auth: dict | None) -> tuple[dict, str | None]:
    header_name = (
        os.environ.get("API_KEY_HEADER")
        or (event_auth or {}).get("header_name")
        or "Authorization"
    )
    prefix = (
        os.environ.get("API_KEY_PREFIX")
        or (event_auth or {}).get("prefix")
        or "Bearer "
    )

    secret_name = os.environ.get("API_SECRET_NAME") or (event_auth or {}).get(
        "secret_name"
    )
    json_key = os.environ.get("API_SECRET_JSON_KEY") or (event_auth or {}).get(
        "json_key"
    )

    if secret_name:
        _log(
            "INFO",
            "auth_resolve",
            "Resolving API key from Secrets Manager",
            secret_name=secret_name,
            json_key=bool(json_key),
        )
        secret = _get_secret_value_with_retry(secret_name, json_key)
        return ({header_name: f"{prefix}{secret}"}, f"secret:{secret_name}")

    # For Testing and Local Development
    env_api_key = os.environ.get("API_KEY")
    if env_api_key:
        return ({header_name: f"{prefix}{env_api_key}"}, "env:API_KEY")

    return ({}, None)


def _http_request(
    method, url, headers=None, query=None, body=None, timeout=10
):
    start_ts = time.monotonic()
    if query:
        qs = urllib.parse.urlencode(query, doseq=True)
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{sep}{qs}"
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers = {**(headers or {}), "Content-Type": "application/json"}
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = body  # bytes
    req = urllib.request.Request(
        url=url, method=method.upper(), headers=headers or {}, data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            return resp.status, dict(resp.headers), resp.read(), duration_ms
    except Exception:
        duration_ms = int((time.monotonic() - start_ts) * 1000)
        raise


def lambda_handler(event, context):
    req = event.get("request", {})
    auth_evt = event.get("auth")
    retry = event.get(
        "retry",
        {
            "max_attempts": 3,
            "backoff": 1.5,
            "retry_on": [429, 500, 502, 503, 504],
        },
    )
    # Normalize retry values in case they arrive as DynamoDB AttributeValue shapes
    max_attempts = int(_as_number(retry.get("max_attempts", 3), 3))
    backoff = float(_as_number(retry.get("backoff", 1.5), 1.5))
    retry_on = set(
        _as_number_list(
            retry.get("retry_on", [429, 500, 502, 503, 504]),
            [429, 500, 502, 503, 504],
        )
    )

    if "url" not in req:
        return {"ok": False, "error": "request.url is required"}

    auth_headers, auth_source = _resolve_api_key(auth_evt)
    headers = _from_attrval(req.get("headers", {})) or {}
    headers.update(auth_headers)
    # Normalize query map as well
    query = (
        _from_attrval(req.get("query"))
        if req.get("query") is not None
        else None
    )
    # Coerce timeout to numeric
    timeout_val = _as_number(req.get("timeout", 10), 10)

    # Log request summary (sanitized headers)
    _log(
        "INFO",
        "request_init",
        "Prepared request",
        method=req.get("method", "GET"),
        url=req.get("url"),
        query=query,
        headers=headers,
        auth_used=auth_source,
        timeout=timeout_val,
    )

    attempt = 0
    last_err = None
    last_status = None
    last_duration_ms = None
    attempt_logs = []
    while attempt < max_attempts:
        attempt += 1
        try:
            status, resp_headers, body, duration_ms = _http_request(
                method=req.get("method", "GET"),
                url=req["url"],
                headers=headers,
                query=query,
                body=req.get("body"),
                timeout=timeout_val,
            )
            last_status = status
            last_duration_ms = duration_ms
            attempt_logs.append(
                {
                    "attempt": attempt,
                    "status": status,
                    "duration_ms": duration_ms,
                }
            )
            _log(
                "INFO",
                "http_response",
                "HTTP response received",
                attempt=attempt,
                status=status,
                duration_ms=duration_ms,
            )
            ok = 200 <= status < 300
            if ok or status not in retry_on:
                content_type = ""
                try:
                    # Headers can have different cases
                    content_type = (
                        resp_headers.get("Content-Type")
                        or resp_headers.get("content-type")
                        or ""
                    )
                except Exception:
                    content_type = ""

                body_text = (
                    body.decode(errors="replace")
                    if isinstance(body, (bytes, bytearray))
                    else str(body)
                )
                truncated = False
                if len(body_text) > _MAX_BODY_CHARS:
                    truncated = True
                    body_text = body_text[:_MAX_BODY_CHARS]
                result = {
                    "ok": ok,
                    "status": status,
                    "headers": resp_headers,
                    "body": body_text,
                    "body_truncated": truncated,
                    "content_type": content_type,
                    "attempts": attempt,
                    "auth_used": auth_source,
                }
                if _RETURN_DEBUG:
                    result["debug"] = {
                        "request": {
                            "method": req.get("method", "GET"),
                            "url": req.get("url"),
                            "query": query,
                            "headers": headers,
                            "timeout": timeout_val,
                        },
                        "attempts": attempt_logs,
                        "last_duration_ms": last_duration_ms,
                    }
                return result
        except Exception as e:
            last_err = str(e)
            attempt_logs.append({"attempt": attempt, "error": last_err})
            _log(
                "ERROR",
                "http_error",
                "HTTP request failed",
                attempt=attempt,
                error=last_err,
            )
        if attempt < max_attempts:
            time.sleep(backoff**attempt)

    result = {
        "ok": False,
        "error": f"Poller failed after {attempt} attempts",
        "last_error": last_err,
        "status": last_status,
        "attempts": attempt,
        "auth_used": auth_source,
    }
    if _RETURN_DEBUG:
        result["debug"] = {
            "request": {
                "method": req.get("method", "GET"),
                "url": req.get("url"),
                "query": query,
                "headers": headers,
                "timeout": timeout_val,
            },
            "attempts": attempt_logs,
            "last_duration_ms": last_duration_ms,
        }
    return result
