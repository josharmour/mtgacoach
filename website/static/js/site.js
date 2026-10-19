// mtgacoach.com: play buttons for the spoken sample lines.
// Without this script each clip keeps its native <audio controls>.
(function () {
    "use strict";
    document.documentElement.classList.add("js");

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
