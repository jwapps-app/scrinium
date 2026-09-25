#!/bin/bash
# Exercises a running helper's request limits: malformed and oversized
# requests must be refused with a status, and the helper must still answer
# /health afterwards. The helper is not started here; run it first.
#
#   ./check.sh [port] [page.png]
#
# With an image the /ocr path is checked too. Two checks only run when the
# helper was started with the matching env:
#   OCR_HELPER_READ_TIMEOUT_SECONDS=2   -> the idle-connection 408
#   OCR_HELPER_MAX_CONCURRENCY=1 OCR_HELPER_MAX_WAITING=1 (and an image)
#                                       -> the 503 when the line is full
# Set the same variables in this shell so the script knows to try them.
set -u
PORT=${1:-9876}
IMAGE=${2:-}
HOST=127.0.0.1
pass=0
fail=0

status_of() {
    # Raw bytes over nc, because curl refuses to send the malformed headers
    # this is about. Prints the status code of the first response line.
    printf '%b' "$1" | nc -w 5 "$HOST" "$PORT" 2>/dev/null | head -1 | tr -d '\r' | awk '{print $2}'
}

check() {
    local name=$1 expect=$2 got=$3
    if [ "$got" = "$expect" ]; then
        echo "ok    $name -> $got"
        pass=$((pass + 1))
    else
        echo "FAIL  $name -> '${got:-no response}' (expected $expect)"
        fail=$((fail + 1))
    fi
}

check "health" 200 "$(status_of 'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')"
check "negative content-length" 400 \
    "$(status_of 'GET /health HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n')"
check "non-numeric content-length" 400 \
    "$(status_of 'POST /ocr HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n')"
check "empty content-length" 400 \
    "$(status_of 'POST /ocr HTTP/1.1\r\nHost: x\r\nContent-Length:\r\n\r\n')"
check "conflicting content-length" 400 \
    "$(status_of 'POST /ocr HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nab')"
check "oversized content-length" 413 \
    "$(status_of 'POST /ocr HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999999999999999\r\n\r\n')"
check "content-length just over the cap" 413 \
    "$(status_of 'POST /ocr HTTP/1.1\r\nHost: x\r\nContent-Length: 67108865\r\n\r\n')"
check "chunked transfer-encoding" 400 \
    "$(status_of 'POST /ocr HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n')"
padding=$(head -c 20000 /dev/zero | tr '\0' a)
check "header section over 16 KiB, no terminator" 431 \
    "$(status_of "GET /health HTTP/1.1\r\nHost: x\r\nX-Pad: $padding")"
check "header section over 16 KiB, terminated" 431 \
    "$(status_of "GET /health HTTP/1.1\r\nHost: x\r\nX-Pad: $padding\r\n\r\n")"
check "unknown route" 404 "$(status_of 'GET /nope HTTP/1.1\r\nHost: x\r\n\r\n')"
check "empty body to /ocr" 422 "$(status_of 'POST /ocr HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n')"
check "health after the malformed requests" 200 \
    "$(status_of 'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')"

if [ -n "$IMAGE" ]; then
    check "/ocr with a real image" 200 \
        "$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: image/png' \
            --data-binary "@$IMAGE" "http://$HOST:$PORT/ocr")"
fi

timeout=${OCR_HELPER_READ_TIMEOUT_SECONDS:-}
if [ -n "$timeout" ] && [ "$timeout" -le 5 ]; then
    # Half a request, then silence with the socket held open (nc half-closes
    # on stdin EOF, which the helper rightly treats as the end of the request
    # rather than as idling): the helper must give up on us.
    got=$( (printf 'GET /health HTTP/1.1\r\nHost: x\r\n'; sleep $((timeout + 3))) \
        | nc -w $((timeout + 3)) "$HOST" "$PORT" 2>/dev/null | head -1 | tr -d '\r' | awk '{print $2}')
    check "idle connection closed after ${timeout}s" 408 "$got"
else
    echo "skip  idle-connection check (start the helper with OCR_HELPER_READ_TIMEOUT_SECONDS=2 and export it here)"
fi

if [ -n "$IMAGE" ] && [ "${OCR_HELPER_MAX_WAITING:-}" = "1" ]; then
    # More requests at once than one slot plus one waiting place can hold.
    codes=$(for _ in $(seq 1 12); do
        curl -s -o /dev/null -w '%{http_code}\n' -H 'Content-Type: image/png' \
            --data-binary "@$IMAGE" "http://$HOST:$PORT/ocr" &
    done; wait)
    if echo "$codes" | grep -q '^503$' && echo "$codes" | grep -q '^200$'; then
        echo "ok    burst of 12 -> $(echo "$codes" | sort | uniq -c | tr -s ' ' | tr '\n' ';')"
        pass=$((pass + 1))
    else
        echo "FAIL  burst of 12 -> $(echo "$codes" | sort | uniq -c | tr -s ' ' | tr '\n' ';') (expected some 503 and some 200)"
        fail=$((fail + 1))
    fi
else
    echo "skip  full-line 503 check (needs an image and OCR_HELPER_MAX_CONCURRENCY=1 OCR_HELPER_MAX_WAITING=1)"
fi

check "health at the end" 200 "$(status_of 'GET /health HTTP/1.1\r\nHost: x\r\n\r\n')"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
