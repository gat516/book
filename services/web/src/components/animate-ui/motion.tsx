// Adapted from Animate UI's Fade and Button primitives (September 2026).
// https://animate-ui.com/r/primitives-effects-fade.json
// https://animate-ui.com/r/primitives-buttons-button.json
// Copyright (c) 2025 Elliot Sutton. License: /licenses/animate-ui.txt.
// React 18 adaptation: native elements; no React 19 ref-as-prop/Slot dependency.
import { motion, useReducedMotion, type HTMLMotionProps } from "motion/react";

export function Fade({ delay = 0, ...props }: HTMLMotionProps<"div"> & { delay?: number }) {
  const reduced = useReducedMotion();
  return <motion.div initial={{ opacity: reduced ? 1 : 0 }} animate={{ opacity: 1 }}
    transition={{ duration: reduced ? 0 : 0.3, delay: reduced ? 0 : delay / 1000 }} {...props} />;
}

export function Button({ hoverScale = 1.02, tapScale = 0.98, disabled, ...props }: HTMLMotionProps<"button"> & { hoverScale?: number; tapScale?: number }) {
  const reduced = useReducedMotion();
  return <motion.button disabled={disabled} whileHover={reduced || disabled ? undefined : { scale: hoverScale }}
    whileTap={reduced || disabled ? undefined : { scale: tapScale }} {...props} />;
}
