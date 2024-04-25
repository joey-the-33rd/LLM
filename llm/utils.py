import click
import httpx
import json
import textwrap
import re
from typing import List, Dict, Generator, Iterable, Optional, Callable


def dicts_to_table_string(
    headings: List[str], dicts: List[Dict[str, str]]
) -> List[str]:
    max_lengths = [len(h) for h in headings]

    # Compute maximum length for each column
    for d in dicts:
        for i, h in enumerate(headings):
            if h in d and len(str(d[h])) > max_lengths[i]:
                max_lengths[i] = len(str(d[h]))

    # Generate formatted table strings
    res = []
    res.append("    ".join(h.ljust(max_lengths[i]) for i, h in enumerate(headings)))

    for d in dicts:
        row = []
        for i, h in enumerate(headings):
            row.append(str(d.get(h, "")).ljust(max_lengths[i]))
        res.append("    ".join(row))

    return res


def remove_dict_none_values(d):
    """
    Recursively remove keys with value of None or value of a dict that is all values of None
    """
    if not isinstance(d, dict):
        return d
    new_dict = {}
    for key, value in d.items():
        if value is not None:
            if isinstance(value, dict):
                nested = remove_dict_none_values(value)
                if nested:
                    new_dict[key] = nested
            elif isinstance(value, list):
                new_dict[key] = [remove_dict_none_values(v) for v in value]
            else:
                new_dict[key] = value
    return new_dict


class _LogResponse(httpx.Response):
    def iter_bytes(self, *args, **kwargs):
        for chunk in super().iter_bytes(*args, **kwargs):
            click.echo(chunk.decode(), err=True)
            yield chunk


class _LogTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport):
        self.transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.transport.handle_request(request)
        return _LogResponse(
            status_code=response.status_code,
            headers=response.headers,
            stream=response.stream,
            extensions=response.extensions,
        )


def _no_accept_encoding(request: httpx.Request):
    request.headers.pop("accept-encoding", None)


def _log_response(response: httpx.Response):
    request = response.request
    click.echo(f"Request: {request.method} {request.url}", err=True)
    click.echo("  Headers:", err=True)
    for key, value in request.headers.items():
        if key.lower() == "authorization":
            value = "[...]"
        if key.lower() == "cookie":
            value = value.split("=")[0] + "=..."
        click.echo(f"    {key}: {value}", err=True)
    click.echo("  Body:", err=True)
    try:
        request_body = json.loads(request.content)
        click.echo(
            textwrap.indent(json.dumps(request_body, indent=2), "    "), err=True
        )
    except json.JSONDecodeError:
        click.echo(textwrap.indent(request.content.decode(), "    "), err=True)
    click.echo(f"Response: status_code={response.status_code}", err=True)
    click.echo("  Headers:", err=True)
    for key, value in response.headers.items():
        if key.lower() == "set-cookie":
            value = value.split("=")[0] + "=..."
        click.echo(f"    {key}: {value}", err=True)
    click.echo("  Body:", err=True)


def logging_client() -> httpx.Client:
    return httpx.Client(
        transport=_LogTransport(httpx.HTTPTransport()),
        event_hooks={"request": [_no_accept_encoding], "response": [_log_response]},
    )

def remove_pref(p:str, r:Iterable[str], store:Optional[List]=None, preproc:Optional[Callable]=None) -> Generator[str,None,None]:
    "Remove prefill `p` from result chunks `r` if the prefill matches the initial chunks"
    # The requirement is that the `p` values should be added to the start of the concatenated list `r`
    # if it's not already there. However, the function is a streaming function which yields one item from
    # `r` at a time -- so we can't actually concatenate `r` first. Instead we have to accrue each part of
    # `r` until we know whether or not it has a prefix which matches `p`, at which point we can start yielding values.
    #
    # To implement this, we iterate through r and accumulate items into `buffer`.
    # At each step, check if the buffer starts with p. Once it does, yield p, then the rest of the buffer (after p)
    # and each subsequent item from r individually.
    buffer = ''
    ir = iter(r)
    has_pre = False
    for x in ir:
        if store is not None: store.append(x)
        if preproc and not isinstance(x,str): x = preproc(x)
        buffer += x
        if buffer.startswith(p):
            buffer = buffer[len(p):]
            has_pre = True
            break
        if not p.startswith(buffer[:len(p)+1]):
            # If we already know the prefill doesn't match the start of the buffer,
            # stop looking.
            break
    yield p
    # LLMs assume that prefill ends at a word boundary, but don't actually add the space char.
    # So if we are adding the prefill, and the prefill doesn't end in whitespace, then also add the space
    if not has_pre and not re.search(r'\s$', p): yield ' '
    yield buffer
    for x in ir:
        if store is not None: store.append(x)
        if preproc and not isinstance(x,str): x = preproc(x)
        yield x

