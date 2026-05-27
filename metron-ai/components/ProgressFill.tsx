'use client';
import { useLayoutEffect, useRef } from 'react';

interface Props {
  /** 0–100 percentage value */
  pct: number;
  /** Tailwind / CSS classes for colour, height, border-radius, etc. */
  className: string;
}

/**
 * Progress bar fill that sets `width` via a DOM ref instead of a `style` prop,
 * eliminating the no-inline-styles lint warning for dynamic values.
 */
export function ProgressFill({ pct, className }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (ref.current) {
      ref.current.style.width = `${Math.min(Math.max(pct, 0), 100)}%`;
    }
  }, [pct]);
  return <div ref={ref} className={className} />;
}
