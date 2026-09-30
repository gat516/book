// Adapted from Animate UI's Fade and Button primitives (September 2026).
// https://animate-ui.com/r/primitives-effects-fade.json
// https://animate-ui.com/r/primitives-buttons-button.json
// Copyright (c) 2025 Elliot Sutton. License: /licenses/animate-ui.txt.
// React 18 adaptation: native elements; no React 19 ref-as-prop/Slot dependency.
import { useId, type ReactNode } from "react";
import { LayoutGroup, motion, useReducedMotion, type HTMLMotionProps } from "motion/react";

export function Fade({ delay = 0, rise = 0, ...props }: HTMLMotionProps<"div"> & { delay?: number; rise?: number }) {
  const reduced = useReducedMotion();
  return <motion.div initial={reduced ? false : { opacity: 0, y: rise }} animate={{ opacity: 1, y: 0 }}
    transition={{ duration: reduced ? 0 : 0.24, delay: reduced ? 0 : delay / 1000 }} {...props} />;
}

// Motion's shared layout keeps the selected background moving between controls.
// Only the decoration persists: chapter-gated content unmounts immediately (§0.3).
export function ChoiceTabs<T extends string>({ value, onChange, choices, className, label }: {
  value: T; onChange: (value: T) => void;
  choices: { value: T; label: string; icon: ReactNode }[];
  className: string; label: string;
}) {
  const id = useId();
  const reduced = useReducedMotion();
  return <LayoutGroup id={id}><div className={`${className} choice-tabs`} role="group" aria-label={label}>
    {choices.map(choice => <Button key={choice.value} type="button" aria-pressed={value === choice.value}
      hoverScale={1} onClick={() => onChange(choice.value)}>
      {value === choice.value && <motion.span className="choice-tab-indicator" aria-hidden="true"
        layoutId={reduced ? undefined : "selected"} transition={{ type: "spring", stiffness: 450, damping: 35 }} />}
      <span className="choice-tab-label">{choice.icon}{choice.label}</span>
    </Button>)}
  </div></LayoutGroup>;
}

export function Button({ hoverScale = 1.02, tapScale = 0.98, disabled, ...props }: HTMLMotionProps<"button"> & { hoverScale?: number; tapScale?: number }) {
  const reduced = useReducedMotion();
  return <motion.button disabled={disabled} whileHover={reduced || disabled ? undefined : { scale: hoverScale }}
    whileTap={reduced || disabled ? undefined : { scale: tapScale }} {...props} />;
}
