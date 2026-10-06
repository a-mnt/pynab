/*
 * Radio player bar, present on every page (see _base.html).
 *
 * The sound comes out of the rabbit, not out of the browser: this bar is a
 * remote control. It asks the server for the radio state and shows itself
 * while a station is playing.
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

  var statusUrl = bar.getAttribute("data-status-url");
  var controlUrl = bar.getAttribute("data-control-url");
  var csrfToken = bar.getAttribute("data-csrf");
  var errorText = bar.getAttribute("data-error");
  var title = bar.querySelector(".js-player-title");
  var buttons = bar.querySelectorAll(".js-player-action");

  var timer = null;
  var current = null;
  var signature = null;

  function render(state) {
    var playing = !!state.is_playing;
    current = state;

    bar.hidden = !playing;
    document.body.classList.toggle("nab-has-player", playing);
    title.textContent = state.station ? state.station.name : "";
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i].getAttribute("data-action") !== "stop") {
        buttons[i].hidden = !state.can_switch;
      }
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

  for (var i = 0; i < buttons.length; i++) {
    buttons[i].addEventListener("click", function (event) {
      var button = event.currentTarget;
      button.disabled = true;
      send(button.getAttribute("data-action"))
        .catch(showError)
        .then(function () {
          button.disabled = false;
        });
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      clearTimeout(timer);
    } else {
      refresh();
    }
  });

  window.NabPlayer = { send: send, refresh: refresh, showError: showError };

  refresh();
})();
