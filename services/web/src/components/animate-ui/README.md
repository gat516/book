# Animate UI adaptations

`motion.tsx` adapts the [Fade](https://animate-ui.com/docs/primitives/effects/fade)
and [Button](https://animate-ui.com/docs/primitives/buttons/button) registry primitives,
downloaded September 20, 2026. The original notice and license are included in
`public/licenses/animate-ui.txt` and distributed with the application.

The application uses React 18. These adaptations use native Motion elements instead
of the upstream React 19 ref/Slot composition, omit unused viewport/list variants,
and honor reduced motion for both opacity and scale. Button scaling is smaller for
a reading interface, and disabled buttons do not animate. No Tailwind or shadcn
project reinitialization is needed. Icons come from `lucide-react`.

The reader and demo use Motion's shared-layout animation for the selected tab
background. Tab content is removed immediately when switching; no outgoing chapter
facts or answers remain during an exit animation (§0.3). Entrance offsets are
limited to cards and setup panels; chapter prose only fades. All motion honors
the system's reduced-motion setting, including the tab indicator and delays.
