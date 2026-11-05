import type { ButtonHTMLAttributes, ReactNode } from "react";

/**
 * Adapted from "Shiny Button" by Hyperiux on 21st.dev.
 * Source: https://21st.dev/@hyperiux/components/shiny-button
 *
 * The original drives a rotating conic-gradient border, a masked speckle
 * field, and an inner sheen. The visual mechanics live in `styles.css`
 * (`.ui-btn-shine`) so every instance shares one set of @property rules and
 * keyframes instead of injecting a <style> block per button.
 */
export function ShinyButton({
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { children: ReactNode }) {
  return (
    <button className={`ui-btn-shine ${className}`.trim()} {...props}>
      <span className="ui-btn-shine-label">{children}</span>
    </button>
  );
}

export default ShinyButton;
