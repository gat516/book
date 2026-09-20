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
