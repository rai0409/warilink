import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Tailwind のクラスを条件つきで結合し、衝突するものは後のものを優先する(shadcn/ui の慣例) */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
