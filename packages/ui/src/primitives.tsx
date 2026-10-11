import * as DialogP from "@radix-ui/react-dialog";
import { Slot } from "@radix-ui/react-slot";
import * as TabsP from "@radix-ui/react-tabs";
import * as TooltipP from "@radix-ui/react-tooltip";
import { cva, type VariantProps } from "class-variance-authority";
import { X } from "lucide-react";
import { forwardRef, type ComponentPropsWithoutRef, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "./cn";

// ------------------------------------------------------------------ Button

export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium min-h-target px-4 " +
    "transition-colors duration-fast focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-focus " +
    "disabled:cursor-not-allowed disabled:opacity-50 [&_svg]:size-[1.1em] [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-teal-solid text-white hover:brightness-110",
        secondary: "border border-line-strong bg-surface text-fg hover:bg-surface-2",
        ghost: "text-fg hover:bg-surface-2",
        danger: "bg-critical-solid text-white hover:brightness-110",
        ai: "bg-violet-solid text-white hover:brightness-110",
      },
      size: { md: "text-base", sm: "text-sm px-3", icon: "px-0 min-w-target" },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps extends ComponentPropsWithoutRef<"button">, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, asChild, ...props }, ref) => {
  const Comp = asChild ? Slot : "button";
  return <Comp ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
});
Button.displayName = "Button";

// ------------------------------------------------------------------ Card

export function Card({ className, ...p }: HTMLAttributes<HTMLElement>) {
  return <section className={cn("rounded-lg border border-line bg-surface shadow-card", className)} {...p} />;
}

export function CardHeader({ title, description, actions, className }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <header className={cn("flex flex-wrap items-start gap-3 border-b border-line px-5 py-4", className)}>
      <div className="min-w-0 flex-1">
        <h2 className="m-0 text-md font-semibold leading-tight">{title}</h2>
        {description && <p className="m-0 mt-1 text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </header>
  );
}

export function CardBody({ className, ...p }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-5 py-4", className)} {...p} />;
}

// ------------------------------------------------------------------ Inputs

const field =
  "min-h-target w-full rounded-md border border-line-strong bg-surface px-3 text-base text-fg placeholder:text-muted " +
  "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-focus disabled:opacity-50";

export const Input = forwardRef<HTMLInputElement, ComponentPropsWithoutRef<"input">>(({ className, ...p }, ref) => (
  <input ref={ref} className={cn(field, className)} {...p} />
));
Input.displayName = "Input";

/** Native select: accessible everywhere, large on touch screens, works with gloves. */
export const NativeSelect = forwardRef<HTMLSelectElement, ComponentPropsWithoutRef<"select">>(({ className, ...p }, ref) => (
  <select ref={ref} className={cn(field, "pr-8", className)} {...p} />
));
NativeSelect.displayName = "NativeSelect";

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="grid gap-1 text-sm">
      <span className="font-medium">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </label>
  );
}

// ------------------------------------------------------------------ Tabs

export const Tabs = TabsP.Root;

export function TabsList({ className, ...p }: ComponentPropsWithoutRef<typeof TabsP.List>) {
  return <TabsP.List className={cn("flex flex-wrap gap-1 border-b border-line", className)} {...p} />;
}

export function TabsTrigger({ className, ...p }: ComponentPropsWithoutRef<typeof TabsP.Trigger>) {
  return (
    <TabsP.Trigger
      className={cn(
        "min-h-target border-b-2 border-transparent px-4 text-sm font-medium text-muted hover:text-fg " +
          "data-[state=active]:border-teal data-[state=active]:text-fg focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-focus",
        className,
      )}
      {...p}
    />
  );
}

export const TabsContent = TabsP.Content;

// ------------------------------------------------------------------ Tooltip

export function Tip({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <TooltipP.Provider delayDuration={300}>
      <TooltipP.Root>
        <TooltipP.Trigger asChild>{children}</TooltipP.Trigger>
        <TooltipP.Portal>
          <TooltipP.Content sideOffset={6} className="z-50 max-w-xs rounded-md bg-slate-900 px-3 py-2 text-sm text-slate-50 shadow-lg">
            {label}
            <TooltipP.Arrow className="fill-slate-900" />
          </TooltipP.Content>
        </TooltipP.Portal>
      </TooltipP.Root>
    </TooltipP.Provider>
  );
}

// ------------------------------------------------------------------ Sheet (side panel dialog)

export function Sheet({ open, onOpenChange, title, description, children, wide }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <DialogP.Root open={open} onOpenChange={onOpenChange}>
      <DialogP.Portal>
        <DialogP.Overlay className="fixed inset-0 z-40 bg-slate-900/40" />
        <DialogP.Content
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-line bg-surface shadow-2xl focus:outline-none",
            wide ? "max-w-6xl" : "max-w-xl",
          )}
        >
          <header className="flex items-start gap-3 border-b border-line px-5 py-4">
            <div className="min-w-0 flex-1">
              <DialogP.Title className="m-0 text-md font-semibold">{title}</DialogP.Title>
              {description && <DialogP.Description className="m-0 mt-1 text-sm text-muted">{description}</DialogP.Description>}
            </div>
            <DialogP.Close asChild>
              <Button variant="ghost" size="icon" aria-label="Close">
                <X />
              </Button>
            </DialogP.Close>
          </header>
          <div className="min-h-0 flex-1 overflow-auto px-5 py-4">{children}</div>
        </DialogP.Content>
      </DialogP.Portal>
    </DialogP.Root>
  );
}

// ------------------------------------------------------------------ Table

export function DataTable({ head, rows, caption, className }: { head: ReactNode[]; rows: ReactNode[][]; caption?: string; className?: string }) {
  return (
    <div className={cn("overflow-x-auto", className)}>
      <table className="w-full border-collapse text-sm">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} scope="col" className="border-b border-line px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="hover:bg-surface-2">
              {r.map((c, j) => (
                <td key={j} className="border-b border-line px-3 py-2 align-top">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ Misc

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden data-loading="" className={cn("animate-pulse rounded-md bg-surface-2", className)} />;
}

export function EmptyState({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="grid place-items-center gap-2 px-6 py-10 text-center">
      {icon && <div className="text-muted [&_svg]:size-8">{icon}</div>}
      <p className="m-0 font-semibold">{title}</p>
      {children && <div className="max-w-md text-sm text-muted">{children}</div>}
    </div>
  );
}

export function Notice({ tone = "advisory", title, children }: { tone?: "advisory" | "warning" | "critical" | "gold"; title?: string; children: ReactNode }) {
  const tones = {
    advisory: "border-advisory bg-advisory-subtle",
    warning: "border-warning bg-warning-subtle",
    critical: "border-critical bg-critical-subtle",
    gold: "border-gold bg-gold-subtle",
  };
  return (
    <div role="note" className={cn("rounded-md border-l-4 px-4 py-3 text-sm", tones[tone])}>
      {title && <p className="m-0 mb-1 font-semibold">{title}</p>}
      {children}
    </div>
  );
}
