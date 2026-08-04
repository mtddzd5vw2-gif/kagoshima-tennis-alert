(() => {
  "use strict";

  const page = document.body.dataset.page;

  // Authentication parameters may contain one-time credentials. Until the
  // Supabase callback is implemented, remove them without reading or logging.
  if (page === "auth-callback" && (window.location.search || window.location.hash)) {
    window.history.replaceState(null, document.title, window.location.pathname);
  }

  const form = document.querySelector("[data-auth-foundation-form]");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const status = form.querySelector("[data-form-status]");
      if (status) {
        status.textContent =
          "認証接続は準備中です。現在はメール送信や会員登録を行いません。";
      }
    });
  }

  document.querySelectorAll("[data-foundation-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.querySelector("[data-action-status]");
      if (target) {
        target.textContent =
          "この操作は準備中です。Supabase接続後に利用できるようになります。";
      }
    });
  });
})();
