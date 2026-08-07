import assert from "node:assert/strict";
import { test } from "node:test";
import {
  classifyResendError,
  deterministicIdempotencyKey,
  escapeHtml,
  hmacPayloadFingerprint,
  renderEmail,
  validHttpUrl,
} from "./helpers.ts";

test("escapeHtml escapes every HTML-sensitive character", () => {
  assert.equal(
    escapeHtml(`<script a="x">'&</script>`),
    "&lt;script a=&quot;x&quot;&gt;&#39;&amp;&lt;/script&gt;",
  );
});

test("validHttpUrl only permits absolute HTTP and HTTPS URLs", () => {
  assert.equal(
    validHttpUrl("https://example.jp/reserve?a=1&b=2"),
    "https://example.jp/reserve?a=1&b=2",
  );
  assert.equal(validHttpUrl("http://example.jp"), "http://example.jp");
  assert.equal(validHttpUrl("javascript:alert(1)"), null);
  assert.equal(validHttpUrl("/relative/path"), null);
  assert.equal(validHttpUrl("not a url"), null);
});

test("renderEmail escapes user-controlled HTML and omits unsafe links", () => {
  const rendered = renderEmail([
    {
      facility_name: `<img src=x onerror="alert(1)">`,
      available_date: "2026-08-08",
      start_time: "09:00:00",
      end_time: "11:00:00",
      payload: {
        court_name: "A&B <Court>",
        reservation_url: "javascript:alert(1)",
      },
    },
  ]);

  assert.match(
    rendered.html,
    /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/,
  );
  assert.match(rendered.html, /A&amp;B &lt;Court&gt;/);
  assert.doesNotMatch(rendered.html, /javascript:/);
  assert.doesNotMatch(rendered.html, /<img/);
});

test("classifyResendError follows retryable and permanent policy", () => {
  assert.deepEqual(classifyResendError(null, null), {
    retryable: true,
    errorCode: "resend_network_error",
  });
  assert.deepEqual(classifyResendError(503, "application_error"), {
    retryable: true,
    errorCode: "resend_server_error",
  });
  assert.deepEqual(classifyResendError(429, "rate_limit_exceeded"), {
    retryable: true,
    errorCode: "resend_rate_limited",
  });
  assert.deepEqual(classifyResendError(429, "daily_quota_exceeded"), {
    retryable: false,
    errorCode: "resend_quota_exceeded",
  });
  assert.deepEqual(classifyResendError(429, "monthly_quota_exceeded"), {
    retryable: false,
    errorCode: "resend_quota_exceeded",
  });
  assert.deepEqual(
    classifyResendError(409, "concurrent_idempotent_requests"),
    {
      retryable: true,
      errorCode: "resend_concurrent_request",
    },
  );
  assert.deepEqual(classifyResendError(409, "invalid_idempotent_request"), {
    retryable: false,
    errorCode: "resend_invalid_idempotent_request",
  });
  assert.deepEqual(classifyResendError(403, "invalid_api_key"), {
    retryable: false,
    errorCode: "resend_invalid_api_key",
  });
  assert.deepEqual(classifyResendError(422, "invalid_from_address"), {
    retryable: false,
    errorCode: "resend_invalid_from",
  });
  assert.deepEqual(classifyResendError(400, "validation_error"), {
    retryable: false,
    errorCode: "resend_validation_error",
  });
});

test("deterministicIdempotencyKey is stable for a message", () => {
  const messageId = "123E4567-E89B-42D3-A456-426614174000";
  const expected =
    "tennis-court-watcher/email/123e4567-e89b-42d3-a456-426614174000";
  assert.equal(deterministicIdempotencyKey(messageId), expected);
  assert.equal(deterministicIdempotencyKey(messageId), expected);
  assert.throws(() => deterministicIdempotencyKey("not-a-uuid"));
});

test("hmacPayloadFingerprint binds the exact payload and secret", async () => {
  const serializedPayload = '{"subject":"空き通知","count":1}';
  const changedPayload = '{"subject":"空き通知","count":2}';
  const secret = "a".repeat(32);
  const changedSecret = `${"a".repeat(31)}b`;

  const [first, repeated, payloadChanged, secretChanged] = await Promise.all([
    hmacPayloadFingerprint(serializedPayload, secret),
    hmacPayloadFingerprint(serializedPayload, secret),
    hmacPayloadFingerprint(changedPayload, secret),
    hmacPayloadFingerprint(serializedPayload, changedSecret),
  ]);

  assert.equal(first, repeated);
  assert.notEqual(first, payloadChanged);
  assert.notEqual(first, secretChanged);
  assert.match(first, /^[0-9a-f]{64}$/);
});
