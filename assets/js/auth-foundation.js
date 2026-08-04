(() => {
  "use strict";

  const LOGIN_PATH = "../auth/login.html";
  const ACCOUNT_PATH = "../account/index.html";

  function getAuthConfig() {
    const config = window.TCW_AUTH_CONFIG;
    if (
      !config ||
      typeof config.supabaseUrl !== "string" ||
      typeof config.supabasePublishableKey !== "string" ||
      typeof config.authCallbackUrl !== "string" ||
      !config.supabaseUrl ||
      !config.supabasePublishableKey ||
      !config.authCallbackUrl
    ) {
      throw new Error("auth_config_unavailable");
    }
    return config;
  }

  function createAuthClient(config) {
    if (!window.supabase || typeof window.supabase.createClient !== "function") {
      throw new Error("auth_sdk_unavailable");
    }

    return window.supabase.createClient(
      config.supabaseUrl,
      config.supabasePublishableKey,
      {
        auth: {
          flowType: "pkce",
          persistSession: true,
          autoRefreshToken: true,
          detectSessionInUrl: false,
        },
      },
    );
  }

  function setStatus(element, message, state = "") {
    if (!element) {
      return;
    }
    element.textContent = message;
    if (state) {
      element.dataset.state = state;
    } else {
      delete element.dataset.state;
    }
  }

  function scrubAuthenticationParameters() {
    if (window.location.search || window.location.hash) {
      window.history.replaceState(null, document.title, window.location.pathname);
    }
  }

  function setupLogin(client, config) {
    const form = document.querySelector("[data-auth-form]");
    if (!form) {
      return;
    }

    const emailInput = form.elements.email;
    const consentInput = form.elements["terms-consent"];
    const submitButton = form.querySelector('button[type="submit"]');
    const status = form.querySelector("[data-form-status]");
    let submitting = false;

    const isValid = () =>
      emailInput.value.trim() !== "" &&
      emailInput.validity.valid &&
      consentInput.checked;

    const updateSubmitState = () => {
      submitButton.disabled = submitting || !isValid();
    };

    emailInput.addEventListener("input", updateSubmitState);
    consentInput.addEventListener("change", updateSubmitState);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (submitting || !isValid()) {
        updateSubmitState();
        return;
      }

      submitting = true;
      updateSubmitState();
      setStatus(status, "送信しています…");

      void client.auth
        .signInWithOtp({
          email: emailInput.value.trim(),
          options: {
            emailRedirectTo: config.authCallbackUrl,
          },
        })
        .then(({ error }) => {
          if (error) {
            throw new Error("magic_link_request_failed");
          }
          form.reset();
          setStatus(
            status,
            "メールを確認してください。ログイン用リンクを送信できる場合は、まもなく届きます。",
            "success",
          );
        })
        .catch(() => {
          setStatus(
            status,
            "送信を完了できませんでした。時間をおいて、もう一度お試しください。",
            "error",
          );
        })
        .finally(() => {
          submitting = false;
          updateSubmitState();
        });
    });

    updateSubmitState();
  }

  async function handleCallback(client) {
    const status = document.querySelector("[data-callback-status]");
    const retry = document.querySelector("[data-callback-retry]");
    const parameters = new URLSearchParams(window.location.search);
    const code = parameters.get("code");

    // Read the one-time code once, then remove every query/fragment value before
    // rendering a result or following any link.
    scrubAuthenticationParameters();

    if (!code) {
      setStatus(
        status,
        "認証リンクを確認できませんでした。ログイン画面からもう一度お試しください。",
        "error",
      );
      retry.hidden = false;
      return;
    }

    setStatus(status, "メール認証を確認しています…");
    try {
      const { error } = await client.auth.exchangeCodeForSession(code);
      if (error) {
        throw new Error("code_exchange_failed");
      }
      window.location.replace(ACCOUNT_PATH);
    } catch {
      setStatus(
        status,
        "認証を完了できませんでした。リンクの期限を確認し、もう一度ログインしてください。",
        "error",
      );
      retry.hidden = false;
    }
  }

  async function setupAccount(client) {
    const loading = document.querySelector("[data-account-loading]");
    const content = document.querySelector("[data-account-content]");
    const email = document.querySelector("[data-account-email]");
    const logout = document.querySelector("[data-sign-out]");
    const status = document.querySelector("[data-action-status]");

    let session;
    try {
      const result = await client.auth.getSession();
      if (result.error) {
        throw new Error("session_lookup_failed");
      }
      session = result.data.session;
    } catch {
      window.location.replace(LOGIN_PATH);
      return;
    }

    if (!session) {
      window.location.replace(LOGIN_PATH);
      return;
    }

    email.textContent = session.user && session.user.email
      ? session.user.email
      : "確認済み";
    loading.hidden = true;
    content.hidden = false;
    logout.disabled = false;

    let signingOut = false;
    logout.addEventListener("click", async () => {
      if (signingOut) {
        return;
      }
      signingOut = true;
      logout.disabled = true;
      setStatus(status, "ログアウトしています…");
      try {
        const { error } = await client.auth.signOut();
        if (error) {
          throw new Error("sign_out_failed");
        }
        email.textContent = "";
        window.location.replace(LOGIN_PATH);
      } catch {
        signingOut = false;
        logout.disabled = false;
        setStatus(
          status,
          "ログアウトを完了できませんでした。時間をおいて、もう一度お試しください。",
          "error",
        );
      }
    });
  }

  async function start() {
    const page = document.body.dataset.page;
    let client;
    let config;

    try {
      config = getAuthConfig();
      client = createAuthClient(config);
    } catch {
      if (page === "account") {
        setStatus(
          document.querySelector("[data-account-loading]"),
          "認証設定を読み込めませんでした。時間をおいて再読み込みしてください。",
          "error",
        );
      } else {
        setStatus(
          document.querySelector("[data-form-status], [data-callback-status]"),
          "認証サービスを利用できません。時間をおいて再読み込みしてください。",
          "error",
        );
      }
      return;
    }

    if (page === "auth-login") {
      setupLogin(client, config);
    } else if (page === "auth-callback") {
      await handleCallback(client);
    } else if (page === "account") {
      await setupAccount(client);
    }
  }

  void start();
})();
