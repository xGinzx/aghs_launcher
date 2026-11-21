# AHB/1 protocol

This protocol connects a local addon to a local Python process through Dota's
console log and KeyValues files. It is separate from the multiplayer network
protocol. Tokens prevent mixing sessions; they do not prove addon compatibility
or protect against another program running as the same local user. Hexadecimal
encoding is not encryption.

## Envelopes

An envelope is `AHB1`, four lowercase hexadecimal digits for the field count,
then one eight-digit lowercase hexadecimal byte length and raw byte value per
field. An empty field is valid. There is no delimiter or trailing data. Lengths
refer to bytes, not Unicode characters. At most 1024 fields are accepted.

Request fields:

1. Uppercase HTTP method.
2. UTF-8 URL.
3. Absolute timeout in decimal milliseconds (1 to 120000).
4. Inactivity timeout in decimal milliseconds (1 to 120000).
5. Body mode: `form` or `raw`.
6. Raw body bytes, or an empty field for form mode.
7. Decimal header count, followed by name/value field pairs.
8. Decimal parameter count, followed by name/value field pairs.

There are at most 64 headers and 128 parameters. Parameters are binary strings
that the Python client percent-encodes. Headers use ASCII names and Latin-1
values, consistent with Python's HTTP/1 client. Transport-managed headers such
as Host and Content-Length cannot be supplied by addon code.

## Request log frames

```text
[AGHS_HTTP_REQUEST] TOKEN:REQUEST_ID:PART/TOTAL:HEX
```

TOKEN is a random 32-character lowercase hexadecimal string generated once per
host launch. REQUEST_ID is `q`, a hexadecimal boot identifier, `_`, and a
hexadecimal sequence number. PART is one-based. Frames contain at most 768 hex
characters. The complete decoded request envelope is limited to 1 MiB. Log
prefixes added by Dota are ignored. Out-of-order frames and identical duplicate
frames are accepted; inconsistent frame counts or duplicates are rejected.

Only frames with the current token are processed. Completed request IDs are
remembered to suppress duplicate sends. State-changing HTTP calls are never
retried automatically.

## Response mailbox

The generated addon's `scripts/aghs_http_bridge.txt` is a KeyValues document with
root `ahb`, token `token`, and optional `probe` flag. The session mailbox is:

```text
scripts/ahb_TOKEN/
```

All response files use the same `ahb` root. `index.kv` contains the session token
and a `done` table mapping completed request IDs to `1`. Python publishes an ID
only after writing all response files atomically.

`REQUEST_ID.kv` contains token, request ID, HTTP code, error string, elapsed
milliseconds, envelope byte length, chunk count, and page count. The response
envelope's fields are: decimal header count, header name/value pairs, and body.
The code is zero on transport failure; genuine HTTP error statuses are preserved.

`REQUEST_ID_PAGE.kv` contains token, request ID, zero-based page number, and a
`data` table. Each page holds up to 64 chunks named `b0` through `b63`. Each chunk
is at most 384 raw bytes represented as lowercase hex. Every chunk except the
last must be exactly 384 bytes. A response body is limited to 4 MiB; the complete
envelope also allows space for headers and framing.

Lua validates the token, request ID, page numbers, dimensions, framing, and byte
length before invoking the original callback with `StatusCode`, `Body`,
`Headers`, `Error`, and `ElapsedMS` fields.

## Acknowledgment

Once Lua finishes reading a response, or times out a request, it prints:

```text
[AGHS_HTTP_ACK] TOKEN:REQUEST_ID
```

Python removes that request's mailbox files and index entry. If the HTTP worker
is still running after a timeout, its eventual result is discarded. A callback
is invoked at most once by the adapter. Session tokens stay fresh even when all
players have identical generated VPKs.
