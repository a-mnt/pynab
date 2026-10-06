/*
 * Radio player bar, always visible on every page (see _base.html).
 *
 * The sound comes out of the rabbit, not out of the browser: this bar is a
 * remote control. It asks the server for the radio state, shows the selected
 * station, and starts or stops it.
 *
 * Other scripts can use:
 *   NabPlayer.send("play" | "stop" | "next" | "previous", stationId)
 *   NabPlayer.refresh()
 * and listen to the "nabradio:status" event on document (event.detail is
 * the state: {is_playing, station: {id, name} | null, can_switch}).
 */
(function () {
  "use strict";

  var bar = document.getElementById("nab-player");
  if (!bar || !window.fetch) {
    return;
  }

  var POLL_PLAYING_MS = 5000;
  var POLL_STOPPED_MS = 15000;
  var AFTER_COMMAND_MS = 2000;
  var STORAGE_KEY = "nabradio.status";

  var statusUrl = bar.getAttribute("data-status-url");
  var controlUrl = bar.getAttribute("data-control-url");
  var csrfToken = bar.getAttribute("data-csrf");
  var errorText = bar.getAttribute("data-error");
  var title = bar.querySelector(".js-player-title");
  var stateText = bar.querySelector(".js-player-state");
  var toggle = bar.querySelector(".js-player-toggle");
  var switchButtons = bar.querySelectorAll(".js-player-action");

  var timer = null;
  var current = null;
  var signature = null;

  function render(state) {
    var playing = !!state.is_playing;
    current = state;

    bar.classList.toggle("is-playing", playing);
    if (state.station) {
      title.textContent = state.station.name;
      stateText.textContent = bar.getAttribute(
        playing ? "data-text-playing" : "data-text-stopped"
      );
    } else {
      title.textContent = bar.getAttribute("aria-label");
      stateText.textContent = bar.getAttribute("data-text-none");
    }
    toggle.disabled = !state.station;
    toggle.setAttribute(
      "aria-label",
      bar.getAttribute(playing ? "data-label-stop" : "data-label-play")
    );
    for (var i = 0; i < switchButtons.length; i++) {
      switchButtons[i].hidden = !state.can_switch;
    }

    // Remembered so that the next page shows the bar right away, before
    // the rabbit has answered.
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {
      /* storage unavailable: not a problem */
    }

    var newSignature = JSON.stringify([playing, state.station]);
    if (newSignature !== signature) {
      signature = newSignature;
      document.dispatchEvent(
        new CustomEvent("nabradio:status", { detail: state })
      );
    }
  }

  function schedule(delay) {
    clearTimeout(timer);
    if (document.hidden) {
      return;
    }
    if (delay === undefined) {
      delay = current && current.is_playing ? POLL_PLAYING_MS : POLL_STOPPED_MS;
    }
    timer = setTimeout(refresh, delay);
  }

  function refresh() {
    return fetch(statusUrl, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(render)
      .catch(function () {
        /* Rabbit unreachable: keep the last known state, try again later. */
      })
      .then(function () {
        schedule();
      });
  }

  function send(action, stationId) {
    var body = new URLSearchParams();
    body.append("action", action);
    if (stationId) {
      body.append("selected_radio", stationId);
    }
    return fetch(controlUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken },
      body: body
    })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            return { ok: response.ok, data: data };
          });
      })
      .then(function (result) {
        if (result.data && "is_playing" in result.data) {
          render(result.data);
        }
        // The rabbit may refuse or fail to play: check again soon.
        schedule(AFTER_COMMAND_MS);
        if (!result.ok) {
          throw new Error(result.data.message || errorText);
        }
        return result.data;
      });
  }

  function showError(error) {
    var message = (error && error.message) || errorText;
    if (window.jQuery && window.jQuery.bootstrapGrowl) {
      window.jQuery.bootstrapGrowl(message, { type: "danger" });
    }
  }

  function onClick(button, getAction) {
    button.addEventListener("click", function () {
      button.disabled = true;
      send(getAction())
        .catch(showError)
        .then(function () {
          button.disabled = !(current && current.station);
        });
    });
  }

  onClick(toggle, function () {
    return current && current.is_playing ? "stop" : "play";
  });
  for (var i = 0; i < switchButtons.length; i++) {
    (function (button) {
      onClick(button, function () {
        return button.getAttribute("data-action");
      });
    })(switchButtons[i]);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      clearTimeout(timer);
    } else {
      refresh();
    }
  });

  window.NabPlayer = { send: send, refresh: refresh, showError: showError };

  // Show the last known state at once, then ask the rabbit.
  try {
    var remembered = window.sessionStorage.getItem(STORAGE_KEY);
    if (remembered) {
      render(JSON.parse(remembered));
    }
  } catch (e) {
    /* nothing remembered */
  }
  refresh();
})();
