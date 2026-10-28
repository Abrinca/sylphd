// API servers for the sylphd web page.
// apiBase: public server used by the published page ("" = none yet; the page
//   then contacts nothing until a server is given with ?api=).
// devApiBase: used instead when the page itself is opened from localhost, so
//   the published page never reaches into the visitor's own machine (browsers
//   ask for "local network access" permission when a public site does that).
// Override per visit with ?api=https://host (e.g. http://127.0.0.1:8765).
window.SYLPHD_CONFIG = {
  apiBase: "",
  devApiBase: "http://127.0.0.1:8765",
};
