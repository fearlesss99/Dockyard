import { useEffect, useRef } from "react";

const FADE_DURATION_MS = 250;
const FADE_OUT_REMAINING_SECONDS = 0.55;
const LOOP_RESET_DELAY_MS = 100;

export function VideoBackground() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const animationFrameRef = useRef<number | null>(null);
  const loopTimerRef = useRef<number | null>(null);
  const fadingOutRef = useRef(false);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let disposed = false;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const showStillFrame = () => {
        video.pause();
        video.currentTime = 0;
        video.style.opacity = "1";
      };
      video.addEventListener("loadeddata", showStillFrame, { once: true });
      if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) showStillFrame();
      return () => video.removeEventListener("loadeddata", showStillFrame);
    }

    const cancelFade = () => {
      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
    };

    const fadeTo = (targetOpacity: number) => {
      if (disposed) return;
      cancelFade();
      const startOpacity = Number.parseFloat(video.style.opacity || "0");
      const startedAt = performance.now();

      const animate = (now: number) => {
        if (disposed) {
          animationFrameRef.current = null;
          return;
        }
        const progress = Math.min((now - startedAt) / FADE_DURATION_MS, 1);
        video.style.opacity = String(startOpacity + (targetOpacity - startOpacity) * progress);
        if (progress < 1) {
          animationFrameRef.current = window.requestAnimationFrame(animate);
        } else {
          animationFrameRef.current = null;
        }
      };

      animationFrameRef.current = window.requestAnimationFrame(animate);
    };

    const fadeIn = () => {
      if (disposed) return;
      fadingOutRef.current = false;
      fadeTo(1);
    };

    const onTimeUpdate = () => {
      if (
        Number.isFinite(video.duration)
        && video.duration - video.currentTime <= FADE_OUT_REMAINING_SECONDS
        && !fadingOutRef.current
      ) {
        fadingOutRef.current = true;
        fadeTo(0);
      }
    };

    const onEnded = () => {
      cancelFade();
      video.style.opacity = "0";
      if (loopTimerRef.current !== null) window.clearTimeout(loopTimerRef.current);
      loopTimerRef.current = window.setTimeout(() => {
        if (disposed) return;
        video.currentTime = 0;
        void video.play().then(() => {
          if (!disposed) fadeIn();
        }, () => undefined);
      }, LOOP_RESET_DELAY_MS);
    };

    video.addEventListener("canplay", fadeIn, { once: true });
    video.addEventListener("timeupdate", onTimeUpdate);
    video.addEventListener("ended", onEnded);
    if (video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) fadeIn();

    return () => {
      disposed = true;
      cancelFade();
      if (loopTimerRef.current !== null) window.clearTimeout(loopTimerRef.current);
      video.removeEventListener("canplay", fadeIn);
      video.removeEventListener("timeupdate", onTimeUpdate);
      video.removeEventListener("ended", onEnded);
    };
  }, []);

  return (
    <div className="video-background" aria-hidden="true">
      <video
        ref={videoRef}
        className="video-background__media"
        src="/dockyard-hero.mp4"
        autoPlay
        muted
        playsInline
        preload="auto"
        style={{ opacity: 0 }}
      />
      <span className="video-background__veil" />
    </div>
  );
}
