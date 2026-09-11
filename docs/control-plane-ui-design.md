# OmniClaw Facilitator Control Plane UI Design

This console is an internal operations surface for a money-moving facilitator. It must feel like a high-trust financial operations system, not a decorative SaaS dashboard.

## Product Role

The control plane is for OmniClaw operators. It is not the seller dashboard.

Operators use it to:

- Read live service, provider, signer, settlement, and reconciliation health.
- Create alpha seller accounts.
- Create payment profiles for seller apps/API surfaces.
- Issue one-time API keys scoped to payment profiles.
- Pause or resume global, provider, and network settlement paths.
- Inspect audit events and authorization state.
- Understand backlog age and operational risk quickly.

The first screen must answer: is the facilitator healthy, are settlements moving, are signers funded, and is anything paused?

## Visual Direction

The UI follows a restrained dark financial console style inspired by Resend and Stripe design principles:

- Near-black background with neutral surfaces.
- Hairline separators and low-alpha borders.
- No decorative grid backgrounds, glow blobs, rainbow gradients, or colorful cards.
- Color is semantic only:
  - Green: settled, healthy, success.
  - Yellow: pending, paused, stale, warning.
  - Red: failed, unauthorized, critical.
  - White/neutral: primary text and active navigation.
- Amounts, counts, timestamps, addresses, network IDs, transaction hashes, and API keys use monospace/tabular typography.
- The brand accent may appear in the OmniClaw mark only, not as general dashboard decoration.

## Typography

Use one UI sans family and one mono family.

- UI font: Inter, Geist, or system sans fallback.
- Mono font: Geist Mono, DM Mono, or system monospace fallback.
- No display font for the console.
- No negative letter spacing.
- Uppercase labels are limited to small metadata labels and table headers.
- Dashboard numbers must use `font-variant-numeric: tabular-nums`.

## Layout

Desktop:

- Fixed left sidebar: 264px.
- Full-height app shell, not a floating centered card.
- Topbar is compact and operational.
- Main content uses one page title row, one KPI strip, then work surfaces.
- Four KPI cards maximum per view.

Mobile:

- Sidebar becomes a horizontal nav strip.
- Tables remain horizontally scrollable.
- Forms remain full width with stable field heights.

## Core Views

Overview:

- Four KPIs max: Records, Submitted, Manual Review, Active Pauses.
- Service health panel.
- Control state panel.
- Provider rail cards only if they explain operational readiness.

Sellers:

- Create seller account.
- Create payment profile with provider, network, asset, and `payTo` policy.
- Issue one-time API key scoped to that profile.
- Show facilitator path and key prefix.
- Show seller -> profile -> API-key hierarchy.
- Label seller/profile refs as operator refs, not values sent to sellers.
- Include a review screen before creating or changing `payTo`.
- Run `/supported` preflight before showing final handoff.
- Generate a seller-safe handoff screen that excludes internal IDs.
- Use separate actions for copying the one-time API key and copying the support prefix.
- Do not expose seller-dashboard concerns here.

Providers:

- Provider health.
- Supported networks.
- Signer gas runway.
- Concurrency cap and lock scope.

Settlements:

- Four KPIs max.
- Status counts.
- Oldest record age by status.
- Provider/network/status breakdown.
- Manual-review risk should be visually obvious.

Controls:

- Pause state.
- Apply audited pause/resume.
- Disabled state must explain why writes are blocked.

Audit:

- Operator, action, target, reason, timestamp.
- Timestamps and IDs in mono.

Security:

- OIDC session.
- OpenFGA authorization state.
- No ops token fallback.

## Interaction Requirements

No interactive element is complete unless it has:

- Default state.
- Hover state.
- Keyboard focus ring.
- Active/pressed state.
- Disabled state.
- Loading state when async work is possible.

Specific rules:

- Buttons use clear hover elevation or border change, not color wash.
- Focus rings are visible and neutral/white, never browser default blue.
- Tables highlight rows on hover.
- Cards lift subtly on hover only if clickable; static metric cards only change border/background slightly.
- Skeletons must match the layout they replace.
- Async buttons must show in-progress labels and remain disabled during submission.

## Anti-Patterns

Do not add:

- Teal/blue/purple accent-heavy generic SaaS palette.
- Gradient top bars on cards.
- Decorative grid background.
- Oversized hero typography.
- Explanatory marketing copy inside the app.
- More widgets to compensate for weak interaction states.
- Seller dashboard flows in the operator console.
