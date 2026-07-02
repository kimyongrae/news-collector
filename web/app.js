const categoryOptions = ["경제", "주식", "금리/환율", "네이버금융", "많이본뉴스_경제", "많이본뉴스_IT", "IT/산업", "부동산"];
const sentimentOptions = ["긍정", "부정", "중립"];
const secretStorageKey = "newsCollector.secretValues";
const legacySecretStorageKey = "marketAlarm.secretValues";
const kakaoAuthCodeStorageKey = "marketAlarm.kakaoAuthCode";
const kakaoAuthErrorStorageKey = "marketAlarm.kakaoAuthError";
const secretKeys = [
  "supabase_url",
  "supabase_service_key",
  "kakao_access_token",
  "kakao_rest_api_key",
  "kakao_redirect_uri",
  "kakao_client_secret",
];
let settings = {};

const $ = (id) => document.getElementById(id);

function renderChips(targetId, options, selected) {
  const target = $(targetId);
  target.innerHTML = "";
  options.forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `chip ${selected.includes(option) ? "active" : ""}`;
    button.textContent = option;
    button.addEventListener("click", () => {
      const listKey = targetId;
      const current = new Set(settings[listKey] || []);
      if (current.has(option)) current.delete(option);
      else current.add(option);
      settings[listKey] = Array.from(current);
      renderChips(targetId, options, settings[listKey]);
    });
    target.appendChild(button);
  });
}

function fillForm(data) {
  settings = data;
  clearBrowserSecretCache();
  $("headline").value = data.headline || "";
  $("provider").value = data.provider || "console";
  $("collect_schedule_times").value = (data.collect_schedule_times || []).join(", ");
  $("schedule_times").value = (data.collect_schedule_times || data.schedule_times || []).join(", ");
  $("collector_config_path").value = data.collector_config_path || "config/categories.yaml";
  $("lookback_hours").value = data.lookback_hours;
  $("min_importance").value = data.min_importance;
  $("max_items").value = data.max_items;
  $("include_ranked").checked = Boolean(data.include_ranked);
  $("include_links").checked = Boolean(data.include_links);
  $("notify_after_collect").checked = Boolean(data.notify_after_collect);
  renderChips("categories", categoryOptions, data.categories || []);
  renderChips("sentiments", sentimentOptions, data.sentiments || []);
  $("sourceStatus").textContent = data.supabase_configured ? "Supabase 연결 설정됨" : "Supabase 미설정";
  $("kakaoStatus").textContent = data.kakao_configured ? "Kakao 토큰 설정됨" : "Kakao 토큰 미설정";
  renderSecretSummary(data);
  renderSecretFields(data);
  restoreKakaoAuthCode();
}

function renderSecretFields(data) {
  const info = data.secret_info || {};
  const values = data.secret_values || {};
  const defs = [
    ["supabase_url", "https://xxxx.supabase.co"],
    ["supabase_service_key", "news_articles 읽기용 키"],
    ["kakao_access_token", "카카오 access token"],
    ["kakao_rest_api_key", "카카오 앱 REST API 키"],
    ["kakao_redirect_uri", "등록한 Redirect URI"],
    ["kakao_client_secret", "사용 설정한 경우에만 입력"],
  ];
  defs.forEach(([key, fallback]) => {
    const input = $(key);
    const pill = $(`${key}_saved`);
    const item = info[key] || {};
    if (input) {
      const defaultValue = key === "kakao_redirect_uri" ? `${window.location.origin}/kakao/callback` : "";
      const value = values[key] || defaultValue;
      input.value = isPlausibleSecretValue(key, value) ? value : "";
      input.placeholder = item.saved ? (item.masked || "•••••••• 저장됨") : fallback;
    }
    if (pill) {
      pill.textContent = item.saved ? `저장됨 ${formatDateShort(item.updated_at)}` : (key === "kakao_client_secret" ? "선택" : "미저장");
      pill.classList.toggle("saved", Boolean(item.saved));
    }
  });
}

function renderSecretSummary(data) {
  $("secretStorage").textContent = data.secret_storage_path || "로컬 SQLite";
  const info = data.secret_info || {};
  const parts = [
    ["Supabase URL", info.supabase_url],
    ["Supabase Key", info.supabase_service_key],
    ["Kakao Token", info.kakao_access_token],
  ].map(([label, item]) => {
    if (!item?.saved) return `${label}: 미저장`;
    return `${label}: 저장됨 (${formatDate(item.updated_at)})`;
  });
  $("secretSavedState").textContent = parts.join(" · ");
}

function restoreKakaoAuthCode() {
  const error = localStorage.getItem(kakaoAuthErrorStorageKey) || "";
  const code = localStorage.getItem(kakaoAuthCodeStorageKey) || "";
  if (error) {
    const resultEl = $("kakao_authorize_result");
    resultEl.className = "inline-result fail";
    resultEl.textContent = `실패 · ${error}`;
    localStorage.removeItem(kakaoAuthErrorStorageKey);
  }
  if (code) {
    $("kakao_auth_code").value = code;
    const resultEl = $("kakao_authorize_result");
    resultEl.className = "inline-result ok";
    resultEl.textContent = "성공 · redirect code가 입력됨";
    localStorage.removeItem(kakaoAuthCodeStorageKey);
  }
}

function formatDateShort(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatDate(value) {
  if (!value) return "시간 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", { hour12: false });
}

function collectForm() {
  return {
    ...settings,
    enabled: $("notify_after_collect").checked,
    headline: $("headline").value,
    provider: $("provider").value,
    schedule_times: $("collect_schedule_times").value.split(",").map((v) => v.trim()).filter(Boolean),
    collect_schedule_times: $("collect_schedule_times").value.split(",").map((v) => v.trim()).filter(Boolean),
    collector_config_path: $("collector_config_path").value.trim() || "config/categories.yaml",
    lookback_hours: Number($("lookback_hours").value),
    min_importance: Number($("min_importance").value),
    max_items: Number($("max_items").value),
    include_ranked: $("include_ranked").checked,
    include_links: $("include_links").checked,
    notify_after_collect: $("notify_after_collect").checked,
  };
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  fillForm(await res.json());
}

async function saveSettings() {
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectForm()),
  });
  fillForm(await res.json());
}

async function saveSecrets() {
  const payload = collectSecrets();
  const res = await fetch("/api/secrets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  fillForm(data);
  $("secretNotice").textContent = `API 키 저장 완료. 저장 위치: ${data.secret_storage_path || "로컬 SQLite"}`;
}

function collectSecrets() {
  return {
    supabase_url: $("supabase_url").value.trim(),
    supabase_service_key: $("supabase_service_key").value.trim(),
    kakao_access_token: $("kakao_access_token").value.trim(),
    kakao_rest_api_key: $("kakao_rest_api_key").value.trim(),
    kakao_redirect_uri: $("kakao_redirect_uri").value.trim(),
    kakao_client_secret: $("kakao_client_secret").value.trim(),
  };
}

function clearBrowserSecretCache() {
  localStorage.removeItem(secretStorageKey);
  localStorage.removeItem(legacySecretStorageKey);
}

function isPlausibleSecretValue(key, value) {
  const text = String(value || "").trim();
  if (!text) return false;
  if (key === "supabase_url") return text.startsWith("http://") || text.startsWith("https://");
  if (key === "supabase_service_key") return text.startsWith("sb_") || text.startsWith("eyJ");
  if (key === "kakao_rest_api_key") return /^[0-9a-fA-F]{32}$/.test(text);
  if (key === "kakao_redirect_uri") return text.startsWith("http://") || text.startsWith("https://");
  if (key === "kakao_client_secret") return !text.startsWith("http://") && !text.startsWith("sb_");
  if (key === "kakao_access_token") {
    return !text.startsWith("http://")
      && !text.startsWith("sb_")
      && !/^[0-9a-fA-F]{32}$/.test(text);
  }
  return false;
}

async function testSecret(key) {
  const resultEl = $(`${key}_result`);
  if (!resultEl) return;
  resultEl.className = "inline-result";
  resultEl.textContent = "테스트 중...";
  const res = await fetch("/api/test-secret", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value: $(key)?.value?.trim() || "", secrets: collectSecrets() }),
  });
  const data = await res.json();
  resultEl.className = `inline-result ${data.ok ? "ok" : "fail"}`;
  resultEl.textContent = `${data.ok ? "성공" : "실패"} · ${data.detail || ""}`;
}

async function createKakaoAuthorizeUrl() {
  const resultEl = $("kakao_authorize_result");
  resultEl.className = "inline-result";
  resultEl.textContent = "생성 중...";
  const payload = collectSecrets();
  const data = await postJson("/api/kakao/authorize-url", payload);
  if (!data.ok) {
    const localUrl = buildKakaoAuthorizeUrl(payload.kakao_rest_api_key, payload.kakao_redirect_uri);
    if (localUrl) {
      $("kakao_authorize_url").value = localUrl;
      $("kakaoAuthorizeLink").href = localUrl;
      resultEl.className = "inline-result ok";
      resultEl.textContent = "성공 · URL 생성됨";
      return;
    }
    resultEl.className = "inline-result fail";
    resultEl.textContent = `실패 · ${formatApiError(data)}`;
    return;
  }
  $("kakao_authorize_url").value = data.authorize_url || "";
  $("kakaoAuthorizeLink").href = data.authorize_url || "#";
  resultEl.className = "inline-result ok";
  resultEl.textContent = "성공 · URL 생성됨";
}

function buildKakaoAuthorizeUrl(restApiKey, redirectUri) {
  const key = String(restApiKey || "").trim();
  const redirect = String(redirectUri || "").trim();
  if (!key || !redirect) return "";
  const params = new URLSearchParams({
    client_id: key,
    response_type: "code",
    redirect_uri: redirect,
    scope: "talk_message",
  });
  return `https://kauth.kakao.com/oauth/authorize?${params.toString()}`;
}

async function exchangeKakaoToken() {
  const resultEl = $("kakao_token_result");
  resultEl.className = "inline-result";
  resultEl.textContent = "발급 중...";
  const authCode = extractKakaoCode($("kakao_auth_code").value.trim());
  const payload = {
    ...collectSecrets(),
    kakao_auth_code: authCode,
  };
  const data = await postJson("/api/kakao/token", payload);
  if (!data.kakao_token_result?.ok) {
    resultEl.className = "inline-result fail";
    resultEl.textContent = `실패 · ${formatApiError(data.kakao_token_result || data)}${formatKakaoRequestInfo(data.request_info)}`;
    return;
  }
  fillForm(data);
  $("kakao_auth_code").value = "";
  resultEl.className = "inline-result ok";
  resultEl.textContent = `성공 · ${data.kakao_token_result.detail || "access token 저장됨"} · 위 Kakao Access Token 칸에 반영됨`;
}

function formatKakaoRequestInfo(info) {
  if (!info) return "";
  const parts = [
    info.redirect_uri ? `redirect_uri=${info.redirect_uri}` : "",
    info.rest_api_key_tail ? `REST API 키 끝=${info.rest_api_key_tail}` : "",
    typeof info.client_secret_used === "boolean" ? `Client Secret ${info.client_secret_used ? "사용" : "미사용"}` : "",
    info.code_length ? `code 길이=${info.code_length}` : "",
  ].filter(Boolean);
  return parts.length ? ` · 요청 확인: ${parts.join(", ")}` : "";
}

function extractKakaoCode(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  try {
    const url = new URL(text);
    return url.searchParams.get("code") || text;
  } catch {
    return text;
  }
}

async function postJson(url, payload) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return {
        ok: false,
        status: res.status,
        detail: data.detail || data.error || `HTTP ${res.status}`,
      };
    }
    return data;
  } catch (error) {
    return { ok: false, detail: `${error.name}: ${error.message}` };
  }
}

function formatApiError(data) {
  const detail = data?.detail || data?.error || "";
  if (data?.status === 404 || detail === "not_found") {
    return "Kakao OAuth API가 아직 서버에 반영되지 않았습니다. NewsCollector 서버를 재시작하세요.";
  }
  return detail || "알 수 없는 오류입니다.";
}

function toggleSecretVisibility(key) {
  const input = $(key);
  const button = document.querySelector(`[data-toggle-secret="${key}"]`);
  if (!input || !button) return;
  const isVisible = input.type === "text";
  input.type = isVisible ? "password" : "text";
  button.classList.toggle("visible", !isVisible);
  button.setAttribute("aria-label", `${button.getAttribute("aria-label")?.replace(/보기|숨기기/g, "") || key} ${isVisible ? "보기" : "숨기기"}`);
  button.title = isVisible ? "입력값 보기" : "입력값 숨기기";
}

async function testConnections() {
  $("connectionResult").classList.add("visible");
  $("connectionResult").textContent = "연결 테스트 중...";
  const res = await fetch("/api/test-connections", { method: "POST" });
  const data = await res.json();
  $("connectionResult").innerHTML = `
    <div><span class="${data.supabase.ok ? "ok" : "fail"}">Supabase</span> ${escapeHtml(data.supabase.detail)}</div>
    <div><span class="${data.kakao.ok ? "ok" : "fail"}">Kakao</span> ${escapeHtml(data.kakao.detail)}</div>
  `;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function preview() {
  $("preview").textContent = "브리핑 생성 중...";
  const res = await fetch("/api/preview");
  const data = await res.json();
  if (data.error) {
    $("preview").textContent = data.error;
    return;
  }
  $("preview").textContent = data.digest.text;
  renderLogs(data.logs || []);
}

async function sendNow() {
  $("preview").textContent = "발송 중...";
  const res = await fetch("/api/send?force=1", { method: "POST" });
  const data = await res.json();
  if (data.error) {
    $("preview").textContent = data.error;
    return;
  }
  $("preview").textContent = data.digest ? data.digest.text : JSON.stringify(data, null, 2);
  await refreshLogs();
}

async function collectNow() {
  const resultEl = $("collectResult");
  resultEl.className = "inline-result";
  resultEl.textContent = "수집 중...";
  await saveSettings();
  const res = await fetch("/api/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notify: $("notify_after_collect").checked }),
  });
  const data = await res.json();
  if (!data.ok) {
    resultEl.className = "inline-result fail";
    resultEl.textContent = `실패 · ${data.error || data.send?.result?.detail || "수집 실패"}`;
    return;
  }
  resultEl.className = "inline-result ok";
  resultEl.textContent = data.send ? "완료 · 수집 후 카카오 발송됨" : "완료 · RSS 수집됨";
  await refreshLogs();
}

async function refreshLogs() {
  const res = await fetch("/api/status");
  const data = await res.json();
  renderLogs(data.logs || []);
}

function renderLogs(logs) {
  const target = $("logs");
  if (!logs.length) {
    target.innerHTML = "<p class='empty'>발송 이력이 없습니다.</p>";
    return;
  }
  target.innerHTML = logs.map((log) => `
    <div class="log-row">
      <span>${log.sent_at}</span>
      <span>${log.provider}</span>
      <span>${log.status}</span>
      <span>${log.item_count}건 ${log.error || ""}</span>
    </div>
  `).join("");
}

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveSettings();
});
$("previewBtn").addEventListener("click", preview);
$("sendBtn").addEventListener("click", sendNow);
$("collectBtn").addEventListener("click", collectNow);
$("saveSecretsBtn").addEventListener("click", saveSecrets);
$("testConnectionsBtn").addEventListener("click", testConnections);
$("kakaoAuthorizeBtn").addEventListener("click", createKakaoAuthorizeUrl);
$("kakaoTokenBtn").addEventListener("click", exchangeKakaoToken);
document.querySelectorAll("[data-test-secret]").forEach((btn) => {
  btn.addEventListener("click", () => testSecret(btn.dataset.testSecret));
});
document.querySelectorAll("[data-toggle-secret]").forEach((btn) => {
  btn.addEventListener("click", () => toggleSecretVisibility(btn.dataset.toggleSecret));
});

loadSettings().then(refreshLogs);
