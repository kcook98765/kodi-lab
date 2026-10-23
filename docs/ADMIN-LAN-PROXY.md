# Administrator HTTPS proxy route guards

The Agents navigation destination is `/mcp-clients`. It is an authenticated
administrator page; agent management continues through `/api/mcp/...`.

An administrator HTTPS proxy that hides the separate MCP transport must match
the endpoint boundary. `location ^~ /mcp` also matches `/mcp-clients`, returning
404 before the application sees the request. Use the reviewed
[route guards](../deployment/admin-lan-route-guards.nginx.conf) inside the
administrator `server` block, alongside the existing `location /` proxy:

```nginx
location = /mcp { return 404; }
location ^~ /mcp/ { return 404; }
location ^~ /native-zip-packages/ { return 404; }
```

Keep the existing TLS listener, source ACL, Host and Origin handling, forwarding
headers, session cookie flags and upstream unchanged. These guards also block
query variants of `/mcp` and its subtree. The administrator listener does not
replace the separately configured [MCP entry point](NATIVE-LAN-MCP.md).

Before applying, preserve the original proxy configuration and verify that its
only change is the route guard replacement. Run `nginx -t` against the complete
candidate configuration, then gracefully reload the existing nginx master.
Verify a fresh HTTPS login and click Agents through that entry point; also
check anonymous Agents access, agent APIs, the other navigation destinations,
blocked MCP/native delivery paths and any separate OpenCode listener. Retain
the original configuration for syntax-checked restoration and graceful reload.

The focused regression in `tests/web/test_admin_lan_proxy.py` runs these exact
guards in a real isolated TLS nginx proxy against the application. It requires
`nginx` and `openssl` on PATH and uses disposable credentials and certificates.
