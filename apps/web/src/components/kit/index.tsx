"use client";

/**
 * UI v2 の共通部品。白地・細い区切り線・小さめの文字・画面下に主ボタン 1 つ、というデザイン案に合わせる。
 * 文字: 本文 14px、補足 12px、画面名 16px、見出し 13px、主金額 28px、行の金額 16px。タップ領域は 44px 以上。
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId } from "react";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------- 画面の枠

/** スマホ幅の 1 画面。広い画面では中央に約 420px で表示する */
export function Screen({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("mx-auto flex min-h-dvh w-full max-w-[420px] flex-col bg-background md:my-6 md:min-h-[calc(100dvh-3rem)] md:rounded-2xl md:shadow-sm", className)}>
      {children}
    </div>
  );
}

/** 画面の本体(左右 24px)。主ボタンの分だけ下に余白を取る */
export function Body({ children, className }: { children: React.ReactNode; className?: string }) {
  return <main className={cn("flex-1 px-6 pb-6", className)}>{children}</main>;
}

/** 上の画面名。左に戻る(または閉じる)、右に任意の操作 */
export function TopBar({
  title,
  subtitle,
  back,
  close,
  right,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  back?: string | true;
  close?: string;
  right?: React.ReactNode;
}) {
  const router = useRouter();
  const nav = (icon: React.ReactNode, label: string, href?: string | true) =>
    typeof href === "string" ? (
      <Link href={href} aria-label={label} className="grid size-11 place-items-center text-foreground">
        {icon}
      </Link>
    ) : (
      <button type="button" aria-label={label} onClick={() => router.back()} className="grid size-11 place-items-center">
        {icon}
      </button>
    );
  return (
    <header className="sticky top-0 z-20 grid grid-cols-[44px_1fr_44px] items-center bg-background/95 px-3 pt-3 pb-2 backdrop-blur">
      <div>
        {close
          ? nav(<X className="size-5" aria-hidden />, "閉じる", close)
          : back
            ? nav(<ChevronLeft className="size-6" aria-hidden />, "戻る", back)
            : null}
      </div>
      <div className="min-w-0 text-center">
        <h1 className="truncate text-[16px] font-semibold">{title}</h1>
        {subtitle && <p className="truncate text-[12px] text-muted-foreground">{subtitle}</p>}
      </div>
      <div className="flex justify-end">{right}</div>
    </header>
  );
}

/** 画面下の主ボタンの置き場所 */
export function BottomBar({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("sticky bottom-0 z-10 space-y-2 bg-background px-6 pt-3 pb-[max(1.5rem,env(safe-area-inset-bottom))]", className)}>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------- ボタン

const BTN = "inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-lg px-4 text-[15px] font-semibold transition-colors disabled:cursor-not-allowed";

export function PrimaryButton({ className, ...props }: React.ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn(BTN, "bg-primary text-primary-foreground hover:bg-[#0756d6] disabled:bg-[#c9cfd8] disabled:text-white", className)}
      {...props}
    />
  );
}

export function OutlineButton({ className, ...props }: React.ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn(BTN, "border border-primary bg-background text-primary hover:bg-secondary disabled:opacity-50", className)}
      {...props}
    />
  );
}

/** 文字だけのボタン(青)。危険な操作は tone="danger" */
export function TextButton({
  className,
  tone = "primary",
  ...props
}: React.ComponentProps<"button"> & { tone?: "primary" | "danger" | "muted" }) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex min-h-11 items-center gap-1.5 text-[14px] font-medium disabled:opacity-50",
        tone === "primary" && "text-primary",
        tone === "danger" && "text-destructive",
        tone === "muted" && "text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

/** リンクをボタンの見た目にする */
export function ButtonLink({
  href,
  variant = "primary",
  className,
  children,
}: {
  href: string;
  variant?: "primary" | "outline";
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className={cn(
        BTN,
        "no-underline",
        variant === "primary" ? "bg-primary text-primary-foreground hover:bg-[#0756d6]" : "border border-primary text-primary hover:bg-secondary",
        className,
      )}
    >
      {children}
    </Link>
  );
}

// ---------------------------------------------------------------- 見出し・行

export function SectionTitle({ children, className }: { children: React.ReactNode; className?: string }) {
  return <h2 className={cn("mt-8 mb-1 text-[13px] font-medium text-muted-foreground", className)}>{children}</h2>;
}

export function Muted({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("text-[12px] text-muted-foreground", className)} {...props} />;
}

export function ErrorText({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <p role="alert" className={cn("my-3 text-[13px] text-destructive", className)}>
      {children}
    </p>
  );
}

export function StatusText({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <p role="status" className={cn("my-3 text-[13px] text-success", className)}>
      {children}
    </p>
  );
}

/** 区切り線つきの一覧 */
export function List({ className, ...props }: React.ComponentProps<"ul">) {
  return <ul className={cn("border-t border-border", className)} {...props} />;
}

/** 一覧の 1 行(左のアイコン・名前・補足・右の値・矢印)。href か onClick があれば押せる */
export function Row({
  icon,
  label,
  sub,
  value,
  href,
  onClick,
  chevron,
  tone,
  className,
}: {
  icon?: React.ReactNode;
  label: React.ReactNode;
  sub?: React.ReactNode;
  value?: React.ReactNode;
  href?: string;
  onClick?: () => void;
  chevron?: boolean;
  tone?: "danger";
  className?: string;
}) {
  const inner = (
    <>
      {icon && <span aria-hidden className={cn("shrink-0 text-foreground [&_svg]:size-5", tone === "danger" && "text-destructive")}>{icon}</span>}
      <span className="min-w-0 flex-1">
        <span className={cn("block truncate text-[14px]", tone === "danger" && "text-destructive")}>{label}</span>
        {sub && <span className="block truncate text-[12px] text-muted-foreground">{sub}</span>}
      </span>
      {value !== undefined && <span className="shrink-0 text-right text-[14px] text-muted-foreground">{value}</span>}
      {(chevron ?? Boolean(href || onClick)) && (
        <ChevronRight aria-hidden className={cn("size-4 shrink-0 text-muted-foreground", tone === "danger" && "text-destructive")} />
      )}
    </>
  );
  const cls = cn("flex min-h-14 w-full items-center gap-4 py-3 text-left", className);
  return (
    <li className="border-b border-border">
      {href ? (
        <Link href={href} className={cn(cls, "text-foreground no-underline")}>{inner}</Link>
      ) : onClick ? (
        <button type="button" onClick={onClick} className={cls}>{inner}</button>
      ) : (
        <div className={cls}>{inner}</div>
      )}
    </li>
  );
}

/** 入力欄(ラベル・消去ボタンつき) */
export function TextField({
  label,
  value,
  onChange,
  onClear,
  className,
  inputClassName,
  ...props
}: Omit<React.ComponentProps<"input">, "onChange"> & {
  label?: string;
  value: string;
  onChange: (v: string) => void;
  onClear?: () => void;
  inputClassName?: string;
}) {
  const id = useId();
  return (
    <div className={cn("mt-6", className)}>
      {label && (
        <label htmlFor={props.id ?? id} className="mb-2 block text-[13px] text-muted-foreground">
          {label}
        </label>
      )}
      <div className="relative">
        <input
          id={props.id ?? id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={cn(
            "h-12 w-full rounded-md border border-input bg-background px-4 text-[15px] outline-none placeholder:text-[#9aa3ae] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/30",
            onClear && "pr-11",
            inputClassName,
          )}
          {...props}
        />
        {onClear && value && (
          <button
            type="button"
            aria-label={`${label ?? "入力"}を消す`}
            onClick={onClear}
            className="absolute top-1/2 right-1 grid size-10 -translate-y-1/2 place-items-center text-muted-foreground"
          >
            <X className="size-4" aria-hidden />
          </button>
        )}
      </div>
    </div>
  );
}

/** ラジオの 1 行(ブラウザ標準の input。読み上げ・テストの check() をそのまま使う) */
export function RadioRow({
  name,
  checked,
  onChange,
  label,
  sub,
  icon,
  right,
  disabled,
}: {
  name: string;
  checked: boolean;
  onChange: () => void;
  label: React.ReactNode;
  sub?: React.ReactNode;
  icon?: React.ReactNode;
  right?: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <li className="border-b border-border">
      <label className={cn("flex min-h-14 cursor-pointer items-center gap-4 py-3", checked && icon && "-mx-3 rounded-lg bg-secondary px-3", disabled && "cursor-not-allowed opacity-60")}>
        <input type="radio" name={name} checked={checked} onChange={onChange} disabled={disabled} className="size-5 accent-[var(--primary)]" />
        {icon && <span aria-hidden className="[&_svg]:size-5">{icon}</span>}
        <span className="min-w-0 flex-1">
          <span className="block text-[14px]">{label}</span>
          {sub && <span className="block text-[12px] text-muted-foreground">{sub}</span>}
        </span>
        {right}
      </label>
    </li>
  );
}

/** チェックボックスの 1 行 */
export function CheckRow({
  checked,
  onChange,
  label,
  sub,
  icon,
  right,
  ariaLabel,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: React.ReactNode;
  sub?: React.ReactNode;
  icon?: React.ReactNode;
  right?: React.ReactNode;
  ariaLabel?: string;
}) {
  return (
    <li className="border-b border-border">
      <label className="flex min-h-14 cursor-pointer items-center gap-4 py-3">
        <input
          type="checkbox"
          checked={checked}
          aria-label={ariaLabel}
          onChange={(e) => onChange(e.target.checked)}
          className="size-5 rounded accent-[var(--primary)]"
        />
        {icon && <span aria-hidden className="[&_svg]:size-5">{icon}</span>}
        <span className="min-w-0 flex-1">
          <span className="block text-[14px]">{label}</span>
          {sub && <span className="block text-[12px] text-muted-foreground">{sub}</span>}
        </span>
        {right}
      </label>
    </li>
  );
}

/** 切り替え(押した状態を aria-pressed で示す) */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className="flex rounded-lg bg-muted p-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
          className={cn(
            "min-h-10 flex-1 rounded-md text-[14px] transition-colors",
            o.value === value ? "bg-primary font-medium text-primary-foreground" : "text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** 1 画面に 1 つだけの大きな金額 */
export function Amount({ label, value, className }: { label?: React.ReactNode; value: string; className?: string }) {
  return (
    <div className={cn("mt-6", className)}>
      {label && <p className="text-[13px] text-muted-foreground">{label}</p>}
      <p className="amount text-[28px] leading-tight font-bold tracking-tight tabular-nums">{value}</p>
    </div>
  );
}

/** 小さな状態表示(未送金・送金の申告あり など) */
export function Pill({ tone = "muted", children }: { tone?: "muted" | "primary" | "success"; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-3 py-0.5 text-[12px]",
        tone === "muted" && "bg-muted text-muted-foreground",
        tone === "primary" && "bg-secondary text-secondary-foreground",
        tone === "success" && "bg-[#e8f6ee] text-success",
      )}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------- ダイアログ・下から出るシート

/** 確認ダイアログ(元に戻せない操作の前に出す) */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  danger,
  busy,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  description?: string;
  confirmLabel: string;
  onConfirm: () => void;
  danger?: boolean;
  busy?: boolean;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/40 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content className="fixed top-1/2 left-1/2 z-50 w-[calc(100%-48px)] max-w-[320px] -translate-x-1/2 -translate-y-1/2 rounded-xl bg-background p-5 text-center shadow-lg">
          <DialogPrimitive.Title className="text-[16px] font-semibold">{title}</DialogPrimitive.Title>
          {description ? (
            <DialogPrimitive.Description className="mt-2 text-[13px] text-muted-foreground">{description}</DialogPrimitive.Description>
          ) : (
            <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
          )}
          <div className="mt-5 grid grid-cols-2 gap-3">
            <DialogPrimitive.Close asChild>
              <button type="button" className="min-h-11 rounded-lg border border-input text-[14px]">
                キャンセル
              </button>
            </DialogPrimitive.Close>
            <button
              type="button"
              disabled={busy}
              onClick={onConfirm}
              className={cn(
                "min-h-11 rounded-lg text-[14px] font-semibold text-white disabled:opacity-60",
                danger ? "bg-destructive" : "bg-primary",
              )}
            >
              {confirmLabel}
            </button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** 画面の下から出るシート(絞り込みなど) */
export function Sheet({
  open,
  onOpenChange,
  title,
  children,
  footer,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <DialogPrimitive.Content className="fixed inset-x-0 bottom-0 z-50 mx-auto w-full max-w-[420px] rounded-t-2xl bg-background px-6 pt-3 pb-[max(1.5rem,env(safe-area-inset-bottom))] shadow-lg">
          <div aria-hidden className="mx-auto mb-3 h-1 w-10 rounded-full bg-[#d6dbe2]" />
          <div className="flex items-center justify-between">
            <DialogPrimitive.Title className="text-[16px] font-semibold">{title}</DialogPrimitive.Title>
            <DialogPrimitive.Close aria-label="閉じる" className="grid size-10 place-items-center">
              <X className="size-5" aria-hidden />
            </DialogPrimitive.Close>
          </div>
          <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
          {children}
          {footer && <div className="mt-6">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** 画面の上に重ねる全画面パネル(支出入力の「分け方」など、戻ると元の画面の入力が残る) */
export function Panel({
  open,
  title,
  subtitle,
  onBack,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  subtitle?: string;
  onBack: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div role="dialog" aria-modal="true" aria-label={title} className="fixed inset-0 z-30 overflow-y-auto bg-board">
      <Screen>
        <header className="sticky top-0 z-20 grid grid-cols-[44px_1fr_44px] items-center bg-background/95 px-3 pt-3 pb-2 backdrop-blur">
          <button type="button" aria-label="戻る" onClick={onBack} className="grid size-11 place-items-center">
            <ChevronLeft className="size-6" aria-hidden />
          </button>
          <div className="min-w-0 text-center">
            <h2 className="truncate text-[16px] font-semibold">{title}</h2>
            {subtitle && <p className="truncate text-[12px] text-muted-foreground">{subtitle}</p>}
          </div>
          <span />
        </header>
        <Body>{children}</Body>
        {footer && <BottomBar>{footer}</BottomBar>}
      </Screen>
    </div>
  );
}
