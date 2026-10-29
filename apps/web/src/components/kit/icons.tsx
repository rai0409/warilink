import { BedDouble, Car, type LucideIcon, Package, ShoppingBasket, Ticket, Utensils, Wine, Receipt } from "lucide-react";

const ICONS: Record<string, LucideIcon> = {
  food: Utensils,
  drinks: Wine,
  groceries: ShoppingBasket,
  transport: Car,
  lodging: BedDouble,
  entertainment: Ticket,
  daily: Package,
};

/** 支出のカテゴリの線画アイコン(飾り。カテゴリ名は別に文字で示す) */
export function CategoryIcon({ category, className }: { category?: string | null; className?: string }) {
  const Icon = (category && ICONS[category]) || Receipt;
  return <Icon aria-hidden className={className ?? "size-5 shrink-0"} strokeWidth={1.6} />;
}
