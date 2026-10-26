(function () {
  // When this page is served by the Flask app, leave this empty.
  // If you host the page elsewhere, set window.SM_API_BASE = "https://your-backend.example.com"
  var API_BASE = (window.SM_API_BASE || "").replace(/\/$/, "");

  var form = document.getElementById("form");
  var urlInput = document.getElementById("url");
  var agree = document.getElementById("agree");
  var goBtn = document.getElementById("go");
  var errorBox = document.getElementById("error");
  var result = document.getElementById("result");
  var thumb = document.getElementById("thumb");
  var titleEl = document.getElementById("title");
  var formatsEl = document.getElementById("formats");

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function setLoading(on) {
    goBtn.disabled = on;
    goBtn.textContent = on ? "Fetching..." : "Get file";
  }

  function render(data) {
    // textContent only: the title comes from a third-party page, never use innerHTML.
    titleEl.textContent = data.title || "Recording";
    if (data.thumbnail) {
      thumb.src = data.thumbnail;
      thumb.hidden = false;
    } else {
      thumb.hidden = true;
    }
    formatsEl.textContent = "";
    data.formats.forEach(function (f) {
      var a = document.createElement("a");
      a.className = "dl " + (f.kind === "video" ? "video" : "audio");
      a.href = API_BASE + f.download_url;
      a.textContent = "Download " + f.label;
      formatsEl.appendChild(a);
    });
    result.hidden = false;
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    errorBox.hidden = true;
    result.hidden = true;

    var url = urlInput.value.trim();
    if (!url) {
      showError("Please paste your recording link.");
      return;
    }
    if (!agree.checked) {
      showError("Please tick the box to confirm you have the right to save this recording.");
      return;
    }

    setLoading(true);
    fetch(API_BASE + "/api/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, confirm: true })
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
          return data;
        });
      })
      .then(render)
      .catch(function (err) {
        showError(err.message || "Network error. Please try again.");
      })
      .then(function () { setLoading(false); });
  });
})();
