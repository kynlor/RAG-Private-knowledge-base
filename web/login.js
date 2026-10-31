const form = document.getElementById("loginForm");
const username = document.getElementById("username");
const password = document.getElementById("password");
const errorBox = document.getElementById("loginError");
const loginBtn = document.getElementById("loginBtn");

username.focus();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  loginBtn.disabled = true;
  loginBtn.textContent = "登录中…";

  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || "登录失败");
    }
    window.location.href = "/";
  } catch (err) {
    errorBox.textContent = err.message;
    loginBtn.disabled = false;
    loginBtn.textContent = "登 录";
    password.focus();
    password.select();
  }
});
