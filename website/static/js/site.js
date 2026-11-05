// mtgacoach.com: play buttons for the spoken sample lines.
// Without this script each clip keeps its native <audio controls>.
(function () {
    "use strict";
    document.documentElement.classList.add("js");

    // A labeled illustration, never a simulated live game or numeric win odds.
    document.querySelectorAll("[data-outlook-demo]").forEach(function (demo) {
        var beam = demo.querySelector("[data-outlook-beam]");
        var label = demo.querySelector("[data-outlook-label]");
        var choices = demo.querySelectorAll("[data-outlook-value]");
        choices.forEach(function (button) {
            button.addEventListener("click", function () {
                choices.forEach(function (choice) {
                    choice.setAttribute("aria-pressed", choice === button ? "true" : "false");
                });
                beam.style.setProperty("--tilt", button.dataset.outlookValue + "deg");
                label.textContent = button.textContent;
            });
        });
    });

    // Match the updater's published version, not the website source revision.
    fetch("/downloads/versions", {credentials: "omit"})
        .then(function (response) {
            if (!response.ok) { throw new Error("Release versions unavailable"); }
            return response.json();
        })
        .then(function (channels) {
            document.querySelectorAll("[data-release-platform]").forEach(function (label) {
                var version = (channels.master || {})[label.dataset.releasePlatform];
                if (/^\d+\.\d+\.\d+$/.test(version || "")) {
                    label.textContent = version + " · Master";
                }
            });
            document.querySelectorAll("[data-release-summary]").forEach(function (label) {
                var lane = label.dataset.releaseSummary;
                var releases = channels[lane] || {};
                var versions = ["win-x64", "osx-arm64", "linux-x64"].map(function (platform) {
                    return releases[platform];
                });
                if (/^\d+\.\d+\.\d+$/.test(versions[0] || "") &&
                        versions.every(function (version) { return version === versions[0]; })) {
                    label.textContent = versions[0] + " · " + (lane === "master" ? "Master" : "Stable");
                }
            });
        })
        .catch(function () { /* Keep accurate channel labels when offline. */ });

    var clips = Array.prototype.slice.call(document.querySelectorAll("[data-clip]"));

    function stopOthers(current) {
        clips.forEach(function (clip) {
            var audio = clip.querySelector("audio");
            if (audio && audio !== current && !audio.paused) {
                audio.pause();
                audio.currentTime = 0;
            }
        });
    }

    clips.forEach(function (clip) {
        var audio = clip.querySelector("audio");
        var button = clip.querySelector(".clip-btn");
        var bar = clip.querySelector(".clip-bar span");
        if (!audio || !button) {
            return;
        }
        var label = button.getAttribute("aria-label") || "Play";
        button.hidden = false;

        function setPlaying(playing) {
            button.setAttribute("aria-pressed", playing ? "true" : "false");
            button.setAttribute("aria-label", playing ? label.replace(/^Play/, "Pause") : label);
        }

        button.addEventListener("click", function () {
            if (audio.paused) {
                stopOthers(audio);
                var started = audio.play();
                if (started && typeof started.catch === "function") {
                    started.catch(function () { setPlaying(false); });
                }
            } else {
                audio.pause();
            }
        });
        audio.addEventListener("play", function () { setPlaying(true); });
        audio.addEventListener("pause", function () { setPlaying(false); });
        audio.addEventListener("ended", function () {
            setPlaying(false);
            audio.currentTime = 0;
            if (bar) { bar.style.width = "0"; }
        });
        audio.addEventListener("timeupdate", function () {
            if (bar && audio.duration) {
                bar.style.width = (100 * audio.currentTime / audio.duration).toFixed(1) + "%";
            }
        });
    });
})();
