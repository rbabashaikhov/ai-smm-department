# Shared by every location that forwards to FastAPI (see default.conf).
#
# proxy_pass takes a variable with no URI part, so the request line reaches
# the API exactly as the browser sent it -- no prefix stripping, no
# rewriting -- and the host is re-resolved per request.

proxy_pass $smm_api;
proxy_http_version 1.1;
proxy_set_header Connection "";

proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Request-ID $smm_request_id;

proxy_redirect off;
proxy_connect_timeout 5s;
proxy_send_timeout 60s;
proxy_read_timeout 60s;

# Only errors nginx generates itself (proxy_intercept_errors is off), i.e.
# when the API cannot be reached at all.
error_page 502 504 = @api_unavailable;
