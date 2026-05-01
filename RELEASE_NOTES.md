## v0.3.6 — Fix handling of redirects by `ws_client.py`

### Reason:
`ws_client.py` had misconfigured `use_ssl` flag, which would hit the websocket at port `80` instead of `443` even though server url was `https//:**`.

And since the client was not configured to handle redirects(`HTTP/1.1 30X Responses`) by server, websocket connection would just fail

### Fix:
This fix handles redirects upto 3 redirects and also properly checks to use ssl or not.