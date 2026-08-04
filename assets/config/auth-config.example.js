/*
 * Public browser configuration template.
 *
 * The publishable key is designed to be exposed in a browser. Its permissions
 * must still be restricted with Row Level Security (RLS).
 *
 * Never put a Supabase secret key, service role key, database password, access
 * token, or private key in this file or in any browser-delivered asset.
 */
window.TCW_AUTH_CONFIG = Object.freeze({
  supabaseUrl: "",
  supabasePublishableKey: "",
  authCallbackUrl: "",
});
