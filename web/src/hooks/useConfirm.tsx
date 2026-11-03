import {
  createContext, useCallback, useContext, useRef, useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";

export interface ConfirmOptions {
  title: string;
  body: string;
  /** Extra word the user must type before confirm enables (typed confirm). */
  typedWord?: string;
  danger?: boolean;
  confirmLabel?: string;
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

interface PendingRequest {
  options: ConfirmOptions;
  resolve: (ok: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }): ReactNode {
  const { t } = useTranslation();
  const [pending, setPending] = useState<PendingRequest | null>(null);
  const [typed, setTyped] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  const confirm = useCallback<ConfirmFn>((options) => {
    return new Promise((resolve) => {
      setTyped("");
      setPending({ options, resolve });
      // Focus the typed input shortly after render.
      if (options.typedWord) {
        setTimeout(() => inputRef.current?.focus(), 50);
      }
    });
  }, []);

  const finish = (ok: boolean) => {
    pending?.resolve(ok);
    setPending(null);
    setTyped("");
  };

  const needsTyped = pending?.options.typedWord !== undefined;
  const typedOk = !needsTyped || typed === pending?.options.typedWord;

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Modal
        open={pending !== null}
        onClose={() => finish(false)}
        title={pending?.options.title ?? ""}
        icon={<AlertTriangle className="h-5 w-5 text-warn" aria-hidden />}
      >
        <p className="text-sm text-slate-300 dark:text-slate-300">{pending?.options.body}</p>
        {needsTyped && (
          <input
            ref={inputRef}
            type="text"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder={pending?.options.typedWord}
            autoComplete="off"
            className="mt-4 w-full rounded-md border border-ink-600 bg-ink-900 px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent"
          />
        )}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => finish(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            variant={pending?.options.danger === false ? "primary" : "danger"}
            disabled={!typedOk}
            onClick={() => finish(true)}
          >
            {pending?.options.confirmLabel ?? t("common.confirm")}
          </Button>
        </div>
      </Modal>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (ctx === null) throw new Error("useConfirm must be used inside ConfirmProvider");
  return ctx;
}
