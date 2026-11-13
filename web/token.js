// Access token of the sylphd web page: stored per server in this browser, sent as a bearer
// token only to the server it was saved for, and checked against the server's X-Token-Valid.
//
// A token lifts the server's rate limit (and opens a server that requires one). It is kept in
// localStorage under one key per server address, so a ?api= override to another server never
// receives it, and it is never sent in the clear to a remote host (plain http only to loopback).
// Loaded before app.js; exposes window.SylphdToken.
(() => {
  'use strict';
  const PREFIX = 'sylphd.token:';
  const LOOPBACK = /^(localhost|127\.0\.0\.1|\[::1\])$/;
  // What an Authorization header can carry safely: printable ASCII, no spaces.
  const VALID_CHARS = /^[\x21-\x7e]+$/;

  // The token of one server address. `status` is 'none' (no token saved), 'unknown' (saved,
  // not yet judged by the server), 'valid' or 'rejected' (the server's last verdict).
  function forServer(api) {
    const key = PREFIX + api;
    let memory = ''; // used when localStorage is unavailable (private mode)
    let status = 'none';
    const listeners = [];

    function read() {
      try { return localStorage.getItem(key) || memory; } catch { return memory; }
    }
    function change(next) {
      if (next === status) return;
      status = next;
      listeners.forEach((f) => f(status));
    }
    if (read()) status = 'unknown';

    function canSend() {
      let u;
      try { u = new URL(api); } catch { return false; }
      return u.protocol === 'https:' || (u.protocol === 'http:' && LOOPBACK.test(u.hostname));
    }

    return {
      get status() { return status; },
      has: () => read() !== '',
      canSend,
      // Saves a token (trimmed); '' forgets it. Returns false for characters a header cannot carry.
      set(value) {
        const token = String(value || '').trim();
        if (token && !VALID_CHARS.test(token)) return false;
        memory = token;
        try {
          if (token) localStorage.setItem(key, token);
          else localStorage.removeItem(key);
        } catch { /* kept in memory for this visit only */ }
        // A new token has no verdict yet; the same one keeps what the server last said.
        status = token ? 'unknown' : 'none';
        listeners.forEach((f) => f(status));
        return true;
      },
      forget() { return this.set(''); },
      // The headers to add to a request to this server (empty without a usable token).
      headers() {
        const token = read();
        return token && canSend() ? { Authorization: `Bearer ${token}` } : {};
      },
      // Feeds the X-Token-Valid response header ('true', 'false' or null when no token was sent).
      note(header) {
        const verdict = header == null ? '' : String(header).trim().toLowerCase();
        if (verdict === 'true') change('valid');
        else if (verdict === 'false') change('rejected');
      },
      onChange: (f) => listeners.push(f),
    };
  }

  // Every token saved by this page, for a "forget all" action: [{ api, token }] without values.
  function savedServers() {
    const servers = [];
    try {
      for (let i = 0; i < localStorage.length; i += 1) {
        const k = localStorage.key(i);
        if (k && k.startsWith(PREFIX)) servers.push(k.slice(PREFIX.length));
      }
    } catch { /* no localStorage */ }
    return servers;
  }
  function forgetAll() {
    savedServers().forEach((api) => { try { localStorage.removeItem(PREFIX + api); } catch { /* ignore */ } });
  }

  window.SylphdToken = { PREFIX, forServer, savedServers, forgetAll };
})();
