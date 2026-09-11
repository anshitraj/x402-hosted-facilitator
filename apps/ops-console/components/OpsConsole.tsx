"use client";

import {
  Activity,
  AlertTriangle,
  Ban,
  BarChart3,
  Bell,
  CheckCircle2,
  CircuitBoard,
  Copy,
  Database,
  ExternalLink,
  Eye,
  Gauge,
  History,
  KeyRound,
  Lock,
  LogIn,
  Network,
  PauseCircle,
  Plus,
  RefreshCw,
  Route,
  Settings,
  ShieldCheck,
  Siren,
  Undo2,
  WalletCards
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import {
  AuditEvent,
  ComponentHealth,
  OpsApiError,
  OpsOverview,
  OpsSession,
  PauseTargetType,
  ReconciliationQueueResponse,
  SellerApiKeySummary,
  SettlementDetailResponse,
  SettlementRecordSummary,
  SellerDetailResponse,
  SellerListItem,
  SellerCreateResponse,
  claimReconciliationRecord,
  createSeller,
  issueSellerApiKey,
  listSellers,
  markReconciliationManualReview,
  readSession,
  readOverview,
  readReconciliationQueue,
  readSeller,
  readSettlement,
  releaseReconciliationRecord,
  revokeSellerApiKey,
  setPause
} from "@/lib/ops-api";

type LoadState = "idle" | "loading" | "ready" | "error";
type AuthState = "checking" | "authenticated" | "unauthenticated";
type OpsView = "overview" | "sellers" | "providers" | "settlements" | "controls" | "audit" | "security";
type ReconciliationAction = "claim" | "release" | "manual-review";

const TARGET_TYPES: PauseTargetType[] = ["global", "provider", "network"];
const DEFAULT_OPERATOR_COHORT = "alpha";
const DEFAULT_RECONCILIATION_REASON = "operator reconciliation triage";
const DEFAULT_NETWORK = "eip155:5042002";
const DEFAULT_ASSET = "0x3600000000000000000000000000000000000000";
const PAYMENT_NETWORKS = [
  {
    id: DEFAULT_NETWORK,
    label: "Arc Testnet",
    usdcAsset: DEFAULT_ASSET,
    assetLabel: "USDC"
  }
] as const;

export function OpsConsole() {
  const [overview, setOverview] = useState<OpsOverview | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [error, setError] = useState<OpsApiError | null>(null);
  const [targetType, setTargetType] = useState<PauseTargetType>("provider");
  const [target, setTarget] = useState("exact_evm");
  const [paused, setPaused] = useState(true);
  const [reason, setReason] = useState("operator maintenance");
  const [submitting, setSubmitting] = useState(false);
  const [session, setSession] = useState<OpsSession>({ authenticated: false });
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [activeView, setActiveView] = useState<OpsView>("overview");
  const [sellerName, setSellerName] = useState("Alpha Seller");
  const [sellerNetwork, setSellerNetwork] = useState(DEFAULT_NETWORK);
  const [sellerPayTo, setSellerPayTo] = useState("");
  const [creatingSeller, setCreatingSeller] = useState(false);
  const [createdSeller, setCreatedSeller] = useState<SellerCreateResponse | null>(null);
  const [issuedApiKey, setIssuedApiKey] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [copyingValue, setCopyingValue] = useState<string | null>(null);
  const [sellers, setSellers] = useState<SellerListItem[]>([]);
  const [sellerDetail, setSellerDetail] = useState<SellerDetailResponse | null>(null);
  const [sellerLoadState, setSellerLoadState] = useState<LoadState>("idle");
  const [selectedSellerRef, setSelectedSellerRef] = useState<string | null>(null);
  const [sellerAction, setSellerAction] = useState<string | null>(null);
  const [settlementDetail, setSettlementDetail] = useState<SettlementDetailResponse | null>(null);
  const [settlementLoadState, setSettlementLoadState] = useState<LoadState>("idle");
  const [selectedSettlementId, setSelectedSettlementId] = useState<number | null>(null);
  const [reconciliationQueue, setReconciliationQueue] = useState<ReconciliationQueueResponse | null>(null);
  const [reconciliationLoadState, setReconciliationLoadState] = useState<LoadState>("idle");
  const [queueStatus, setQueueStatus] = useState("all");
  const [queueMinAgeSeconds, setQueueMinAgeSeconds] = useState(0);
  const [queueSellerRef, setQueueSellerRef] = useState("");
  const [queueProvider, setQueueProvider] = useState("");
  const [queueNetwork, setQueueNetwork] = useState("");
  const [reconciliationAction, setReconciliationAction] = useState<string | null>(null);
  const sellerRequestId = useRef(0);
  const settlementRequestId = useRef(0);
  const reconciliationRequestId = useRef(0);
  const [authError] = useState<string | null>(() =>
    typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("auth_error")
  );

  const refresh = useCallback(async () => {
    setLoadState((current) => (current === "ready" ? current : "loading"));
    setError(null);
    try {
      const next = await readOverview();
      setOverview(next);
      setLoadState("ready");
    } catch (caught) {
      const normalized = normalizeError(caught);
      if (normalized.status === 401 || normalized.status === 403) {
        setSession({ authenticated: false });
        setAuthState("unauthenticated");
      }
      setError(normalized);
      setLoadState("error");
    }
  }, []);

  const refreshSellers = useCallback(async () => {
    const requestId = sellerRequestId.current + 1;
    sellerRequestId.current = requestId;
    const detailSellerRef = selectedSellerRef;
    setSellerLoadState((current) => (current === "ready" ? current : "loading"));
    try {
      const nextSellers = await listSellers();
      if (sellerRequestId.current !== requestId) {
        return;
      }
      setSellers(nextSellers);
      setSellerLoadState("ready");
      if (detailSellerRef && nextSellers.some((seller) => seller.sellerRef === detailSellerRef)) {
        const detail = await readSeller(detailSellerRef);
        if (sellerRequestId.current !== requestId) {
          return;
        }
        setSellerDetail(detail);
      }
    } catch (caught) {
      if (sellerRequestId.current !== requestId) {
        return;
      }
      setError(normalizeError(caught));
      setSellerLoadState("error");
    }
  }, [selectedSellerRef]);

  const refreshReconciliationQueue = useCallback(async () => {
    const requestId = reconciliationRequestId.current + 1;
    reconciliationRequestId.current = requestId;
    setReconciliationLoadState("loading");
    setReconciliationQueue(null);
    setError(null);
    try {
      const nextQueue = await readReconciliationQueue({
        status: queueStatus,
        minAgeSeconds: queueMinAgeSeconds,
        sellerRef: queueSellerRef.trim(),
        provider: queueProvider.trim(),
        network: queueNetwork.trim(),
        limit: 50
      });
      if (reconciliationRequestId.current !== requestId) {
        return;
      }
      setReconciliationQueue(nextQueue);
      setReconciliationLoadState("ready");
    } catch (caught) {
      if (reconciliationRequestId.current !== requestId) {
        return;
      }
      setReconciliationQueue(null);
      setError(normalizeError(caught));
      setReconciliationLoadState("error");
    }
  }, [queueMinAgeSeconds, queueNetwork, queueProvider, queueSellerRef, queueStatus]);

  useEffect(() => {
    let active = true;
    let interval: number | undefined;

    void readSession()
      .then((nextSession) => {
        if (!active) {
          return;
        }
        setSession(nextSession);
        if (!nextSession.authenticated) {
          setAuthState("unauthenticated");
          setLoadState("idle");
          return;
        }
        setAuthState("authenticated");
        void refresh();
        interval = window.setInterval(() => void refresh(), 30_000);
      })
      .catch(() => {
        if (!active) {
          return;
        }
        setSession({ authenticated: false });
        setAuthState("unauthenticated");
        setLoadState("idle");
      });

    return () => {
      active = false;
      if (interval) {
        window.clearInterval(interval);
      }
    };
  }, [refresh]);

  useEffect(() => {
    if (authState === "authenticated" && activeView === "sellers") {
      const timeout = window.setTimeout(() => void refreshSellers(), 0);
      return () => window.clearTimeout(timeout);
    }
    return undefined;
  }, [activeView, authState, refreshSellers]);

  useEffect(() => {
    if (authState === "authenticated" && activeView === "settlements") {
      const timeout = window.setTimeout(() => void refreshReconciliationQueue(), 0);
      return () => window.clearTimeout(timeout);
    }
    return undefined;
  }, [activeView, authState, refreshReconciliationQueue]);

  useEffect(() => {
    if (!issuedApiKey) {
      return undefined;
    }
    const timeout = window.setTimeout(() => {
      setIssuedApiKey(null);
      setCopyStatus("Secret cleared automatically");
    }, 120_000);
    return () => window.clearTimeout(timeout);
  }, [issuedApiKey]);

  const providerOptions = useMemo(() => overview?.providers.names ?? [], [overview]);
  const networkOptions = useMemo(
    () => Array.from(new Set([...(overview?.providers.networks ?? []), ...(overview?.pauseState.networks ?? [])])).sort(),
    [overview]
  );
  const canWrite = overview?.pauseState.writesEnabled === true;
  const activePauseCount =
    (overview?.pauseState.globalPaused ? 1 : 0) +
    (overview?.pauseState.providers.length ?? 0) +
    (overview?.pauseState.networks.length ?? 0);

  function chooseTargetType(type: PauseTargetType) {
    setTargetType(type);
    if (type === "global") {
      return;
    }
    setTarget(type === "provider" ? providerOptions[0] ?? "exact_evm" : networkOptions[0] ?? "eip155:5042002");
  }

  async function submitPause(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const result = await setPause({
        targetType,
        target: targetType === "global" ? undefined : target.trim(),
        paused,
        reason: reason.trim()
      });
      setOverview((current) =>
        current
          ? {
              ...current,
              pauseState: result.pauseState,
              auditTail: [result.event, ...current.auditTail].slice(0, 25)
            }
          : current
      );
      await refresh();
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setSubmitting(false);
    }
  }

  async function submitSeller(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreatingSeller(true);
    setCreatedSeller(null);
    setIssuedApiKey(null);
    setCopyStatus(null);
    setError(null);
    try {
      const paymentNetwork = paymentNetworkFor(sellerNetwork);
      const result = await createSeller({
        sellerRef: deriveSellerReference(sellerName),
        tenantRef: DEFAULT_OPERATOR_COHORT,
        name: sellerName.trim() || undefined,
        environment: "testnet",
        enabledNetworks: [paymentNetwork.id],
        allowedAssets: [paymentNetwork.usdcAsset],
        allowedPayTo: [sellerPayTo.trim()]
      });
      setIssuedApiKey(result.apiKey);
      setCreatedSeller({ ...result, apiKey: "" });
      if (result.auditEvent) {
        setOverview((current) =>
          current
            ? {
                ...current,
                auditTail: [result.auditEvent as AuditEvent, ...current.auditTail].slice(0, 25)
              }
            : current
        );
      }
      setActiveView("sellers");
      setSelectedSellerRef(result.seller.sellerRef);
      setSellerDetail(await readSeller(result.seller.sellerRef));
      await refreshSellers();
      await refresh();
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setCreatingSeller(false);
    }
  }

  async function selectSeller(sellerRef: string) {
    const requestId = sellerRequestId.current + 1;
    sellerRequestId.current = requestId;
    setSelectedSellerRef(sellerRef);
    setSellerAction(null);
    setSellerDetail(null);
    setError(null);
    try {
      setSellerLoadState("loading");
      const detail = await readSeller(sellerRef);
      if (sellerRequestId.current !== requestId) {
        return;
      }
      setSellerDetail(detail);
      setSellerLoadState("ready");
    } catch (caught) {
      if (sellerRequestId.current !== requestId) {
        return;
      }
      setError(normalizeError(caught));
      setSellerLoadState("error");
    }
  }

  async function selectSettlement(recordId: number) {
    const requestId = settlementRequestId.current + 1;
    settlementRequestId.current = requestId;
    setSelectedSettlementId(recordId);
    setSettlementDetail(null);
    setSettlementLoadState("loading");
    setError(null);
    try {
      const detail = await readSettlement(recordId);
      if (settlementRequestId.current !== requestId) {
        return;
      }
      setSettlementDetail(detail);
      setSettlementLoadState("ready");
    } catch (caught) {
      if (settlementRequestId.current !== requestId) {
        return;
      }
      setError(normalizeError(caught));
      setSettlementLoadState("error");
    }
  }

  async function submitReconciliationAction(
    recordId: number,
    action: ReconciliationAction,
    reason: string
  ) {
    setReconciliationAction(`${action}:${recordId}`);
    setError(null);
    try {
      const payload = { reason: reason.trim(), leaseSeconds: 300 };
      const result =
        action === "claim"
          ? await claimReconciliationRecord(recordId, payload)
          : action === "release"
            ? await releaseReconciliationRecord(recordId, payload)
            : await markReconciliationManualReview(recordId, payload);
      setSettlementDetail({ record: result.record, attempts: result.attempts });
      if (result.auditEvent) {
        setOverview((current) =>
          current
            ? {
                ...current,
                auditTail: [result.auditEvent as AuditEvent, ...current.auditTail].slice(0, 25)
              }
            : current
        );
      }
      await refreshReconciliationQueue();
      await refresh();
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setReconciliationAction(null);
    }
  }

  async function issueKeyForSelectedSeller(paymentProfileId = "default") {
    if (!selectedSellerRef) {
      return;
    }
    setSellerAction(`issue:${selectedSellerRef}`);
    setIssuedApiKey(null);
    setCreatedSeller(null);
    setCopyStatus(null);
    setError(null);
    try {
      const result = await issueSellerApiKey(selectedSellerRef, paymentProfileId);
      setIssuedApiKey(result.apiKey);
      const detail = await readSeller(selectedSellerRef);
      setSellerDetail(detail);
      await refreshSellers();
      if (result.auditEvent) {
        setOverview((current) =>
          current
            ? {
                ...current,
                auditTail: [result.auditEvent as AuditEvent, ...current.auditTail].slice(0, 25)
              }
            : current
        );
      }
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setSellerAction(null);
    }
  }

  async function revokeKeyForSelectedSeller(key: SellerApiKeySummary, revokeReason: string) {
    if (!selectedSellerRef || key.status !== "active") {
      return;
    }
    setSellerAction(`revoke:${key.keyId}`);
    setError(null);
    try {
      const result = await revokeSellerApiKey(selectedSellerRef, key.keyId, revokeReason);
      setSellerDetail(await readSeller(selectedSellerRef));
      await refreshSellers();
      if (result.auditEvent) {
        setOverview((current) =>
          current
            ? {
                ...current,
                auditTail: [result.auditEvent as AuditEvent, ...current.auditTail].slice(0, 25)
              }
            : current
        );
      }
    } catch (caught) {
      setError(normalizeError(caught));
    } finally {
      setSellerAction(null);
    }
  }

  if (authState !== "authenticated") {
    return <AuthGate state={authState} error={authError} />;
  }

  const systemOk = overview?.status === "ok";
  const isRefreshing = loadState === "loading";

  return (
    <main className="dashboardShell">
      <header className="dashboardTopbar">
        <div className="topbarBrand">
          OmniClaw
          <span>Facilitator</span>
        </div>
        <div className="topbarActions">
          <span className={`badge badgeDot ${systemOk ? "badgeGreen" : "badgeYellow"}`}>
            {overview ? (systemOk ? "All systems operational" : "Attention required") : "Loading status"}
          </span>
          <button
            className={`iconButton ${isRefreshing ? "isSpinning" : ""}`}
            type="button"
            onClick={() => void refresh()}
            aria-label="Refresh"
            aria-busy={isRefreshing}
            disabled={isRefreshing}
          >
            <RefreshCw size={18} />
          </button>
          <button className="iconButton" type="button" onClick={() => setActiveView("audit")} aria-label="Audit trail">
            <Bell size={18} />
          </button>
          <button className="iconButton" type="button" onClick={() => setActiveView("security")} aria-label="Security settings">
            <Settings size={18} />
          </button>
          <div className="sessionBadge">
            <ShieldCheck size={15} />
            <span>{session.email ?? session.subject ?? "Authenticated"}</span>
          </div>
          <div className="avatar">{initials(session.email ?? session.subject)}</div>
          <form action="/api/auth/logout" method="post">
            <button className="secondaryButton" type="submit">Sign out</button>
          </form>
        </div>
      </header>

      <div className="dashboardBody">
        <aside className="dashboardSidebar" aria-label="Control plane navigation">
          <SidebarNav activeView={activeView} onSelect={setActiveView} />
        </aside>

        <section className="dashboardContent">
          <div className="pageTitleRow">
            <div>
              <p className="eyebrow">Internal control plane</p>
              <h1>{viewTitle(activeView)}</h1>
              <p className="pageSubtitle">{viewSubtitle(activeView)}</p>
            </div>
            <span className="livePill">Live data</span>
          </div>

          {error ? <StatusNotice error={error} /> : null}
          {activeView === "overview" ? (
            <OverviewView
              overview={overview}
              activePauseCount={activePauseCount}
              onOpenControls={() => setActiveView("controls")}
            />
          ) : null}
          {activeView === "sellers" ? (
            <SellersView
              sellerName={sellerName}
              sellerNetwork={sellerNetwork}
              sellerPayTo={sellerPayTo}
              creatingSeller={creatingSeller}
              createdSeller={createdSeller}
              issuedApiKey={issuedApiKey}
              sellers={sellers}
              sellerDetail={sellerDetail}
              sellerLoadState={sellerLoadState}
              selectedSellerRef={selectedSellerRef}
              selectedSettlementId={selectedSettlementId}
              sellerAction={sellerAction}
              copyStatus={copyStatus}
              copyingValue={copyingValue}
              onSellerNameChange={setSellerName}
              onSellerNetworkChange={setSellerNetwork}
              onSellerPayToChange={setSellerPayTo}
              onCopyStatusChange={setCopyStatus}
              onCopyingValueChange={setCopyingValue}
              onClearIssuedApiKey={() => setIssuedApiKey(null)}
              onRefreshSellers={refreshSellers}
              onSelectSeller={selectSeller}
              onSelectSettlement={(recordId) => {
                setActiveView("settlements");
                void selectSettlement(recordId);
              }}
              onIssueApiKey={issueKeyForSelectedSeller}
              onRevokeApiKey={revokeKeyForSelectedSeller}
              onSubmit={submitSeller}
            />
          ) : null}
          {activeView === "providers" ? <ProvidersView overview={overview} /> : null}
          {activeView === "settlements" ? (
            <SettlementsView
              overview={overview}
              settlementDetail={settlementDetail}
              settlementLoadState={settlementLoadState}
              selectedSettlementId={selectedSettlementId}
              reconciliationQueue={reconciliationQueue}
              reconciliationLoadState={reconciliationLoadState}
              queueStatus={queueStatus}
              queueMinAgeSeconds={queueMinAgeSeconds}
              queueSellerRef={queueSellerRef}
              queueProvider={queueProvider}
              queueNetwork={queueNetwork}
              reconciliationAction={reconciliationAction}
              currentOperatorKeys={[session.operatorKey, session.subject].filter(Boolean) as string[]}
              providerOptions={providerOptions}
              networkOptions={networkOptions}
              onQueueStatusChange={setQueueStatus}
              onQueueMinAgeSecondsChange={setQueueMinAgeSeconds}
              onQueueSellerRefChange={setQueueSellerRef}
              onQueueProviderChange={setQueueProvider}
              onQueueNetworkChange={setQueueNetwork}
              onRefreshQueue={refreshReconciliationQueue}
              onSelectSettlement={selectSettlement}
              onSelectSeller={(sellerRef) => {
                setActiveView("sellers");
                void selectSeller(sellerRef);
              }}
              onSubmitReconciliationAction={submitReconciliationAction}
            />
          ) : null}
          {activeView === "controls" ? (
            <ControlsView
              overview={overview}
              targetType={targetType}
              target={target}
              paused={paused}
              reason={reason}
              submitting={submitting}
              canWrite={canWrite}
              providerOptions={providerOptions}
              networkOptions={networkOptions}
              onChooseTargetType={chooseTargetType}
              onTargetChange={setTarget}
              onPausedChange={setPaused}
              onReasonChange={setReason}
              onSubmit={submitPause}
            />
          ) : null}
          {activeView === "audit" ? <AuditView overview={overview} /> : null}
          {activeView === "security" ? <SecurityView overview={overview} session={session} /> : null}
        </section>
      </div>
    </main>
  );
}

function SidebarNav({
  activeView,
  onSelect
}: {
  activeView: OpsView;
  onSelect: (view: OpsView) => void;
}) {
  return (
    <nav className="sidebarNav">
      <p className="navSectionLabel">Operations</p>
      <NavButton active={activeView === "overview"} icon={<BarChart3 size={16} />} label="Overview" onClick={() => onSelect("overview")} />
      <NavButton active={activeView === "sellers"} icon={<KeyRound size={16} />} label="Sellers" onClick={() => onSelect("sellers")} />
      <NavButton active={activeView === "providers"} icon={<Route size={16} />} label="Providers" onClick={() => onSelect("providers")} />
      <NavButton active={activeView === "settlements"} icon={<WalletCards size={16} />} label="Settlements" onClick={() => onSelect("settlements")} />
      <NavButton active={activeView === "controls"} icon={<PauseCircle size={16} />} label="Pauses" onClick={() => onSelect("controls")} />
      <p className="navSectionLabel">Assurance</p>
      <NavButton active={activeView === "audit"} icon={<History size={16} />} label="Audit Trail" onClick={() => onSelect("audit")} />
      <p className="navSectionLabel">Security</p>
      <NavButton active={activeView === "security"} icon={<ShieldCheck size={16} />} label="OIDC / OpenFGA" onClick={() => onSelect("security")} />
    </nav>
  );
}

function NavButton({
  active,
  icon,
  label,
  onClick
}: {
  active: boolean;
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button className={`navItem ${active ? "active" : ""}`} type="button" onClick={onClick} aria-current={active ? "page" : undefined}>
      {icon}
      {label}
    </button>
  );
}

function viewTitle(view: OpsView): string {
  return {
    overview: "Operations Overview",
    sellers: "Seller Onboarding",
    providers: "Providers & Networks",
    settlements: "Settlement & Reconciliation",
    controls: "Emergency Controls",
    audit: "Audit Trail",
    security: "Security & Access"
  }[view];
}

function viewSubtitle(view: OpsView): string {
  return {
    overview: "Live service health, settlement counts, and readiness for the hosted facilitator.",
    sellers: "Create alpha seller accounts and issue one-time facilitator API keys.",
    providers: "Configured facilitator rails and supported network state.",
    settlements: "Durable settlement records, attempts, status counts, and manual-review backlog.",
    controls: "Apply or clear global, provider, and network pauses. Every change is audited.",
    audit: "Recent control-plane changes written by the backend audit store.",
    security: "Current operator session and enforced OIDC/OpenFGA authorization boundaries."
  }[view];
}

function OverviewView({
  overview,
  activePauseCount,
  onOpenControls
}: {
  overview: OpsOverview | null;
  activePauseCount: number;
  onOpenControls: () => void;
}) {
  return (
    <>
      <SummaryMetrics overview={overview} activePauseCount={activePauseCount} />
      <section className="workGrid">
        <HealthPanel overview={overview} />
        <div className="panel">
          <div className="panelHeader">
            <div>
              <p className="eyebrow">Current pauses</p>
              <h2>Control State</h2>
            </div>
            <PauseCircle size={20} aria-hidden="true" />
          </div>
          <PauseStateView overview={overview} />
          <button className="secondaryButton panelAction" type="button" onClick={onOpenControls}>
            Open pause controls
          </button>
        </div>
      </section>
      <RailCards overview={overview} />
    </>
  );
}

function SellersView({
  sellerName,
  sellerNetwork,
  sellerPayTo,
  creatingSeller,
  createdSeller,
  issuedApiKey,
  sellers,
  sellerDetail,
  sellerLoadState,
  selectedSellerRef,
  selectedSettlementId,
  sellerAction,
  copyStatus,
  copyingValue,
  onSellerNameChange,
  onSellerNetworkChange,
  onSellerPayToChange,
  onCopyStatusChange,
  onCopyingValueChange,
  onClearIssuedApiKey,
  onRefreshSellers,
  onSelectSeller,
  onSelectSettlement,
  onIssueApiKey,
  onRevokeApiKey,
  onSubmit
}: {
  sellerName: string;
  sellerNetwork: string;
  sellerPayTo: string;
  creatingSeller: boolean;
  createdSeller: SellerCreateResponse | null;
  issuedApiKey: string | null;
  sellers: SellerListItem[];
  sellerDetail: SellerDetailResponse | null;
  sellerLoadState: LoadState;
  selectedSellerRef: string | null;
  selectedSettlementId: number | null;
  sellerAction: string | null;
  copyStatus: string | null;
  copyingValue: string | null;
  onSellerNameChange: (value: string) => void;
  onSellerNetworkChange: (value: string) => void;
  onSellerPayToChange: (value: string) => void;
  onCopyStatusChange: (value: string | null) => void;
  onCopyingValueChange: (value: string | null) => void;
  onClearIssuedApiKey: () => void;
  onRefreshSellers: () => Promise<void>;
  onSelectSeller: (sellerRef: string) => Promise<void>;
  onSelectSettlement: (recordId: number) => void;
  onIssueApiKey: (paymentProfileId?: string) => Promise<void>;
  onRevokeApiKey: (key: SellerApiKeySummary, reason: string) => Promise<void>;
  onSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>;
}) {
  const createdProfile = createdSeller?.seller.paymentProfiles?.[0];
  const policyNetworks = createdProfile?.enabledNetworks.length
    ? createdProfile.enabledNetworks
    : createdSeller?.seller.enabledNetworks ?? [];
  const policyPayTo = createdProfile?.allowedPayTo.length
    ? createdProfile.allowedPayTo
    : createdSeller?.seller.allowedPayTo ?? [];

  return (
    <section className="sellersStack">
    <div className="workGrid sellersGrid">
      <div className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Seller account</p>
            <h2>Create Seller</h2>
          </div>
          <KeyRound size={20} aria-hidden="true" />
        </div>
        <form className="sellerForm" onSubmit={onSubmit}>
          <label className="field">
            <span>Name</span>
            <input
              value={sellerName}
              onChange={(event) => onSellerNameChange(event.target.value)}
              maxLength={120}
              required
            />
          </label>
          <div className="fieldGrid single">
            <label className="field">
              <span>Network</span>
              <select value={sellerNetwork} onChange={(event) => onSellerNetworkChange(event.target.value)} required>
                {PAYMENT_NETWORKS.map((network) => (
                  <option key={network.id} value={network.id}>
                    {network.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="field">
            <span>Seller Pay To</span>
            <input
              value={sellerPayTo}
              onChange={(event) => onSellerPayToChange(event.target.value)}
              pattern="0x[a-fA-F0-9]{40}"
              placeholder="0x..."
              required
            />
          </label>
          <button className="primaryButton" type="submit" disabled={creatingSeller}>
            <Plus size={17} />
            {creatingSeller ? "Creating" : "Create seller"}
          </button>
        </form>
      </div>
      <div className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Issued credentials</p>
            <h2>Facilitator Access</h2>
          </div>
          <ShieldCheck size={20} aria-hidden="true" />
        </div>
        {createdSeller ? (
          <div className="issuedAccess">
            <div className="notice secretNotice" role="status">
              <KeyRound size={18} />
              <span>The raw API key is shown once. Copy it into the seller handoff, then clear it from this screen.</span>
            </div>
            <div className="statusList">
              <div className="statusRow"><span>Key Prefix</span><strong className="mono">{createdSeller.apiKeyPrefix}</strong></div>
            </div>
            <div className="keyBox">
              <span>API Key</span>
              {issuedApiKey ? (
                <>
                  <code>{issuedApiKey}</code>
                  <div className="keyActions">
                    <button
                      className="secondaryButton"
                      type="button"
                      disabled={copyingValue === issuedApiKey}
                      aria-busy={copyingValue === issuedApiKey}
                      onClick={() => void copyText(issuedApiKey, onCopyStatusChange, onCopyingValueChange)}
                    >
                      <Copy size={16} />
                      {copyingValue === issuedApiKey ? "Copying" : "Copy key"}
                    </button>
                  <button className="secondaryButton" type="button" onClick={onClearIssuedApiKey}>
                      Clear secret
                    </button>
                  </div>
                </>
              ) : (
                <p className="inlineHint">Secret cleared. Only the key prefix remains in audit and seller records.</p>
              )}
              {copyStatus ? <p className="inlineHint">{copyStatus}</p> : null}
            </div>
            <div className="statusList">
              <div className="statusRow"><span>Network</span><strong>{policyNetworks.map(networkLabel).join(", ")}</strong></div>
              <div className="statusRow"><span>Policy Pay To</span><strong className="mono">{policyPayTo.join(", ")}</strong></div>
            </div>
            <div className="keyBox">
              <span>Facilitator Path</span>
              <code>{createdSeller.facilitatorPath}</code>
              <button
                className="secondaryButton"
                type="button"
                disabled={copyingValue === createdSeller.facilitatorPath}
                aria-busy={copyingValue === createdSeller.facilitatorPath}
                onClick={() => void copyText(createdSeller.facilitatorPath, onCopyStatusChange, onCopyingValueChange)}
              >
                <Copy size={16} />
                {copyingValue === createdSeller.facilitatorPath ? "Copying" : "Copy path"}
              </button>
            </div>
          </div>
        ) : (
          <div className="emptyState">
            <KeyRound size={24} />
            <p>No seller issued in this session.</p>
          </div>
        )}
      </div>
    </div>
    <SellerManagementPanel
      sellers={sellers}
      sellerDetail={sellerDetail}
      sellerLoadState={sellerLoadState}
      selectedSellerRef={selectedSellerRef}
      selectedSettlementId={selectedSettlementId}
      sellerAction={sellerAction}
      issuedApiKey={createdSeller ? null : issuedApiKey}
      copyStatus={copyStatus}
      copyingValue={copyingValue}
      onRefreshSellers={onRefreshSellers}
      onSelectSeller={onSelectSeller}
      onSelectSettlement={onSelectSettlement}
      onIssueApiKey={onIssueApiKey}
      onRevokeApiKey={onRevokeApiKey}
      onCopyStatusChange={onCopyStatusChange}
      onCopyingValueChange={onCopyingValueChange}
      onClearIssuedApiKey={onClearIssuedApiKey}
    />
    </section>
  );
}

function SellerManagementPanel({
  sellers,
  sellerDetail,
  sellerLoadState,
  selectedSellerRef,
  selectedSettlementId,
  sellerAction,
  issuedApiKey,
  copyStatus,
  copyingValue,
  onRefreshSellers,
  onSelectSeller,
  onSelectSettlement,
  onIssueApiKey,
  onRevokeApiKey,
  onCopyStatusChange,
  onCopyingValueChange,
  onClearIssuedApiKey
}: {
  sellers: SellerListItem[];
  sellerDetail: SellerDetailResponse | null;
  sellerLoadState: LoadState;
  selectedSellerRef: string | null;
  selectedSettlementId: number | null;
  sellerAction: string | null;
  issuedApiKey: string | null;
  copyStatus: string | null;
  copyingValue: string | null;
  onRefreshSellers: () => Promise<void>;
  onSelectSeller: (sellerRef: string) => Promise<void>;
  onSelectSettlement: (recordId: number) => void;
  onIssueApiKey: (paymentProfileId?: string) => Promise<void>;
  onRevokeApiKey: (key: SellerApiKeySummary, reason: string) => Promise<void>;
  onCopyStatusChange: (value: string | null) => void;
  onCopyingValueChange: (value: string | null) => void;
  onClearIssuedApiKey: () => void;
}) {
  return (
    <section className="widePanel">
      <div className="panelHeader">
        <div>
          <p className="eyebrow">Seller registry</p>
          <h2>Manage Sellers</h2>
        </div>
        <button
          className="iconButton"
          type="button"
          onClick={() => void onRefreshSellers()}
          aria-label="Refresh sellers"
          aria-busy={sellerLoadState === "loading"}
        >
          <RefreshCw size={17} />
        </button>
      </div>
      <div className="sellerManagementGrid">
        <div className="sellerListPane">
          {sellerLoadState === "loading" && sellers.length === 0 ? (
            <SkeletonRows count={5} />
          ) : sellers.length ? (
            <div className="sellerList">
              {sellers.map((seller) => (
                <button
                  key={seller.sellerRef}
                  className={`sellerListItem ${selectedSellerRef === seller.sellerRef ? "active" : ""}`}
                  type="button"
                  onClick={() => void onSelectSeller(seller.sellerRef)}
                >
                  <span>
                    <strong>{seller.name || seller.sellerRef}</strong>
                    <em className="mono">{seller.sellerRef}</em>
                  </span>
                  <span className="sellerListMeta">
                    {formatNumber(seller.activeApiKeyCount)} active key{seller.activeApiKeyCount === 1 ? "" : "s"}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="emptyState compact">
              <KeyRound size={22} />
              <p>No sellers found.</p>
            </div>
          )}
        </div>
        <div className="sellerDetailPane">
          {sellerLoadState === "loading" && !sellerDetail ? (
            <div className="sellerDetail">
              <SkeletonRows count={6} />
            </div>
          ) : sellerDetail ? (
            <SellerDetail
              detail={sellerDetail}
              selectedSettlementId={selectedSettlementId}
              sellerAction={sellerAction}
              issuedApiKey={issuedApiKey}
              copyStatus={copyStatus}
              copyingValue={copyingValue}
              onIssueApiKey={onIssueApiKey}
              onSelectSettlement={onSelectSettlement}
              onRevokeApiKey={onRevokeApiKey}
              onCopyStatusChange={onCopyStatusChange}
              onCopyingValueChange={onCopyingValueChange}
              onClearIssuedApiKey={onClearIssuedApiKey}
            />
          ) : (
            <div className="emptyState compact">
              <ShieldCheck size={22} />
              <p>Select a seller to manage keys and profiles.</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function SellerDetail({
  detail,
  selectedSettlementId,
  sellerAction,
  issuedApiKey,
  copyStatus,
  copyingValue,
  onIssueApiKey,
  onSelectSettlement,
  onRevokeApiKey,
  onCopyStatusChange,
  onCopyingValueChange,
  onClearIssuedApiKey
}: {
  detail: SellerDetailResponse;
  selectedSettlementId: number | null;
  sellerAction: string | null;
  issuedApiKey: string | null;
  copyStatus: string | null;
  copyingValue: string | null;
  onIssueApiKey: (paymentProfileId?: string) => Promise<void>;
  onSelectSettlement: (recordId: number) => void;
  onRevokeApiKey: (key: SellerApiKeySummary, reason: string) => Promise<void>;
  onCopyStatusChange: (value: string | null) => void;
  onCopyingValueChange: (value: string | null) => void;
  onClearIssuedApiKey: () => void;
}) {
  const defaultProfile = detail.seller.paymentProfiles?.[0];
  const [revokeReason, setRevokeReason] = useState("operator requested key rotation");
  const canRevokeWithReason = revokeReason.trim().length >= 3;
  return (
    <div className="sellerDetail">
      <div className="statusList">
        <div className="statusRow"><span>Seller</span><strong>{detail.seller.name ?? detail.seller.sellerRef}</strong></div>
        <div className="statusRow"><span>Reference</span><strong className="mono">{detail.seller.sellerRef}</strong></div>
        <div className="statusRow"><span>Status</span><strong>{detail.seller.status}</strong></div>
      </div>
      <SellerHealthSummary detail={detail} />
      <div className="detailSectionHeader">
        <h3>Payment Profiles</h3>
      </div>
      <div className="statusList">
        {(detail.seller.paymentProfiles ?? []).map((profile) => (
          <div className="statusRow profileRow" key={profile.paymentProfileId}>
            <span>{profile.name}</span>
            <strong>{profile.status} · {profile.enabledNetworks.map(networkLabel).join(", ")}</strong>
            <span className="rowDetail mono">{profile.allowedPayTo.join(", ")}</span>
          </div>
        ))}
      </div>
      <div className="detailSectionHeader">
        <h3>API Keys</h3>
        <button
          className="secondaryButton"
          type="button"
          disabled={sellerAction?.startsWith("issue:")}
          aria-busy={sellerAction?.startsWith("issue:")}
          onClick={() => void onIssueApiKey(defaultProfile?.paymentProfileId ?? "default")}
        >
          <Plus size={16} />
          {sellerAction?.startsWith("issue:") ? "Issuing" : "Issue key"}
        </button>
      </div>
      <label className="field compactField">
        <span>Revoke Reason</span>
        <input
          value={revokeReason}
          onChange={(event) => setRevokeReason(event.target.value)}
          minLength={3}
          maxLength={240}
          required
        />
      </label>
      {issuedApiKey ? (
        <div className="keyBox">
          <span>New API Key</span>
          <code>{issuedApiKey}</code>
          <div className="keyActions">
            <button
              className="secondaryButton"
              type="button"
              disabled={copyingValue === issuedApiKey}
              aria-busy={copyingValue === issuedApiKey}
              onClick={() => void copyText(issuedApiKey, onCopyStatusChange, onCopyingValueChange)}
            >
              <Copy size={16} />
              {copyingValue === issuedApiKey ? "Copying" : "Copy key"}
            </button>
            <button className="secondaryButton" type="button" onClick={onClearIssuedApiKey}>
              Clear secret
            </button>
          </div>
          {copyStatus ? <p className="inlineHint">{copyStatus}</p> : null}
        </div>
      ) : null}
      {detail.apiKeys.length ? (
        <div className="tableWrap sellerKeyTable">
          <table>
            <thead>
              <tr>
                <th>Prefix</th>
                <th>Profile</th>
                <th>Status</th>
                <th>Created</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {detail.apiKeys.map((key) => (
                <tr key={key.keyId}>
                  <td className="mono">{key.keyPrefix}</td>
                  <td className="mono">{key.paymentProfileId}</td>
                  <td>
                    <span className={`statusBadge ${key.status === "active" ? "good" : "neutral"}`}>
                      {key.status}
                    </span>
                  </td>
                  <td className="mono">{formatTime(key.createdAt)}</td>
                  <td>
                    <button
                      className="secondaryButton compactButton"
                      type="button"
                      disabled={key.status !== "active" || !canRevokeWithReason || sellerAction === `revoke:${key.keyId}`}
                      aria-busy={sellerAction === `revoke:${key.keyId}`}
                      onClick={() => void onRevokeApiKey(key, revokeReason)}
                    >
                      {sellerAction === `revoke:${key.keyId}` ? "Revoking" : "Revoke"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">No API keys recorded for this seller.</p>
      )}
      <SellerRecentSettlements
        records={detail.settlement.recentRecords}
        selectedRecordId={selectedSettlementId}
        onSelectSettlement={onSelectSettlement}
      />
    </div>
  );
}

function SellerHealthSummary({ detail }: { detail: SellerDetailResponse }) {
  const settlement = detail.settlement;
  const risk = settlement.reconciliationRisk ?? {
    backlog: 0,
    active: 0,
    staleActive: 0,
    manualReview: 0,
    unknown: 0,
    oldestAgeSeconds: 0
  };
  const newestApiKey = detail.apiKeys[0]?.createdAt ?? "";
  const newestRevoked = detail.apiKeys.find((key) => key.revokedAt)?.revokedAt ?? "";
  return (
    <section className="sellerHealthGrid" aria-label="Seller settlement health">
      <Metric label="Settled" value={formatNumber(settlement.settled)} icon={<CheckCircle2 size={18} />} tone="good" />
      <Metric label="Unknown" value={formatNumber(settlement.unknown)} icon={<AlertTriangle size={18} />} tone={settlement.unknown > 0 ? "warn" : "neutral"} />
      <Metric label="Manual Review" value={formatNumber(settlement.manualReviewBacklog)} icon={<Siren size={18} />} tone={settlement.manualReviewBacklog > 0 ? "warn" : "neutral"} />
      <Metric label="Settled Volume" value={settlement.totalAmountUsdc ? `${settlement.totalAmountUsdc} USDC` : "0 USDC"} icon={<WalletCards size={18} />} />
      <div className="sellerStatusGroups">
        <div className="sellerStatusGroup">
          <h4>Settlement Counts</h4>
          <div className="statusList sellerActivityList">
            <div className="statusRow"><span>Records</span><strong className="mono">{formatNumber(settlement.records)}</strong></div>
            <div className="statusRow"><span>Attempts</span><strong className="mono">{formatNumber(settlement.attempts)}</strong></div>
            <div className="statusRow"><span>Submitted</span><strong className="mono">{formatNumber(settlement.submitted)}</strong></div>
            <div className="statusRow"><span>Failed</span><strong className="mono">{formatNumber(settlement.failed)}</strong></div>
          </div>
        </div>
        <div className="sellerStatusGroup">
          <h4>Reconciliation Risk</h4>
          <div className="statusList sellerActivityList">
            <div className="statusRow"><span>Queue Backlog</span><strong className="mono">{formatNumber(risk.backlog)}</strong></div>
            <div className="statusRow"><span>Submitted/In Progress</span><strong className="mono">{formatNumber(risk.active)}</strong></div>
            <div className="statusRow"><span>Stale In Flight</span><strong className="mono">{formatNumber(risk.staleActive)}</strong></div>
            <div className="statusRow"><span>Oldest Queue Item</span><strong className="mono">{risk.oldestAgeSeconds ? formatDuration(risk.oldestAgeSeconds) : "none"}</strong></div>
          </div>
        </div>
        <div className="sellerStatusGroup">
          <h4>Activity</h4>
          <div className="statusList sellerActivityList">
            <div className="statusRow"><span>Last Update</span><strong className="mono">{settlement.lastUpdatedAt ? formatTime(settlement.lastUpdatedAt) : "none"}</strong></div>
            <div className="statusRow"><span>Last Settlement</span><strong className="mono">{settlement.lastSettlementAt ? formatTime(settlement.lastSettlementAt) : "none"}</strong></div>
            <div className="statusRow"><span>Newest Key</span><strong className="mono">{newestApiKey ? formatTime(newestApiKey) : "none"}</strong></div>
            <div className="statusRow"><span>Newest Revoked Key</span><strong className="mono">{newestRevoked ? formatTime(newestRevoked) : "none"}</strong></div>
          </div>
        </div>
      </div>
    </section>
  );
}

function SellerRecentSettlements({
  records,
  selectedRecordId,
  onSelectSettlement
}: {
  records: SettlementRecordSummary[];
  selectedRecordId: number | null;
  onSelectSettlement: (recordId: number) => void;
}) {
  const handleKeyboardSelect = (
    event: KeyboardEvent<HTMLTableRowElement>,
    recordId: number
  ) => {
    if (event.target !== event.currentTarget) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelectSettlement(recordId);
    }
  };
  return (
    <section className="sellerRecentSettlements">
      <div className="detailSectionHeader">
        <h3>Recent Money Movement</h3>
      </div>
      {records.length ? (
        <div className="tableWrap sellerSettlementTable">
          <table>
            <thead>
              <tr>
                <th>Record</th>
                <th>Status</th>
                <th>Rail</th>
                <th>Amount</th>
                <th>Updated</th>
                <th>Transaction</th>
                <th>Open</th>
              </tr>
            </thead>
            <tbody>
              {records.map((record) => (
                <tr
                  key={record.recordId}
                  className={selectedRecordId === record.recordId ? "selectedRow" : ""}
                  role="button"
                  tabIndex={0}
                  aria-pressed={selectedRecordId === record.recordId}
                  aria-label={`Open settlement record ${record.recordId}`}
                  onClick={() => onSelectSettlement(record.recordId)}
                  onKeyDown={(event) => handleKeyboardSelect(event, record.recordId)}
                >
                  <td>
                    <button
                      className="linkButton mono"
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        onSelectSettlement(record.recordId);
                      }}
                    >
                      {record.recordId}
                    </button>
                  </td>
                  <td>
                    <span className={`statusBadge ${settlementStatusTone(record.status)}`}>
                      {labelize(record.status)}
                    </span>
                  </td>
                  <td>
                    <strong>{settlementRailLabel(record)}</strong>
                    <span className="cellMeta">{networkLabel(record.network)}</span>
                  </td>
                  <td className="mono">{record.amountUsdc ? `${record.amountUsdc} USDC` : record.amountAtomic || "unknown"}</td>
                  <td className="mono">{record.updatedAt ? formatTime(record.updatedAt) : "unknown"}</td>
                  <td className="mono">{record.transaction ? shorten(record.transaction) : record.errorReason || "pending"}</td>
                  <td>
                    <button
                      className="secondaryButton compactButton"
                      type="button"
                      aria-label={`Open settlement ${record.recordId}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        onSelectSettlement(record.recordId);
                      }}
                    >
                      <Eye size={15} />
                      Inspect
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">No settlement records for this seller.</p>
      )}
    </section>
  );
}

function ProvidersView({ overview }: { overview: OpsOverview | null }) {
  const providerItems = overview?.providers.items ?? [];
  const signerItems = providerItems.filter((provider) =>
    provider.signerStatus !== undefined ||
    provider.balanceWei !== undefined ||
    provider.minBalanceWei !== undefined ||
    provider.maxConcurrentSettlements !== undefined ||
    provider.signerLockScope !== undefined
  );
  return (
    <>
      <RailCards overview={overview} />
      <section className="widePanel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Provider router</p>
            <h2>Configured Providers</h2>
          </div>
          <Route size={20} aria-hidden="true" />
        </div>
        {overview ? (
          <div className="statusList">
            {(providerItems.length ? providerItems : overview.providers.names.map((name) => ({ name, status: overview.providers.status }))).map((provider) => (
              <div className="statusRow" key={provider.name}>
                <span>{provider.name}</span>
                <strong>{provider.status}</strong>
              </div>
            ))}
          </div>
        ) : (
          <SkeletonRows count={3} />
        )}
      </section>
      <section className="widePanel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Signer runway</p>
            <h2>Gas & Balance</h2>
          </div>
          <Gauge size={20} aria-hidden="true" />
        </div>
        {signerItems.length ? (
          <div className="statusList">
            {signerItems.map((provider) => (
              <div className="statusRow signerRow" key={`${provider.name}-signer`}>
                <span>{signerProviderLabel(provider.name)}</span>
                <strong>
                  {provider.signerStatus ?? "unknown"}
                  {provider.network ? ` · ${networkLabel(provider.network)}` : ""}
                </strong>
                <span className="rowDetail">
                  {provider.balanceWei !== undefined ? `${formatWei(provider.balanceWei)} wei` : "balance unavailable"}
                  {provider.minBalanceWei !== undefined ? ` / floor ${formatWei(provider.minBalanceWei)} wei` : ""}
                  {provider.maxConcurrentSettlements !== undefined ? ` · cap ${provider.maxConcurrentSettlements}` : ""}
                  {provider.signerLockScope ? ` · lock ${provider.signerLockScope}` : ""}
                  {provider.supportedNetworks?.length ? ` · ${provider.supportedNetworks.map(networkLabel).join(", ")}` : ""}
                  {provider.errorType ? ` · ${provider.errorType}` : ""}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className="emptyState">
            <Gauge size={24} />
            <p>No gas-backed signer providers are reporting balance metadata.</p>
          </div>
        )}
      </section>
      <section className="widePanel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Networks</p>
            <h2>Paused Networks</h2>
          </div>
          <Network size={20} aria-hidden="true" />
        </div>
        <PauseStateView overview={overview} />
      </section>
    </>
  );
}

function SettlementsView({
  overview,
  settlementDetail,
  settlementLoadState,
  selectedSettlementId,
  reconciliationQueue,
  reconciliationLoadState,
  queueStatus,
  queueMinAgeSeconds,
  queueSellerRef,
  queueProvider,
  queueNetwork,
  reconciliationAction,
  currentOperatorKeys,
  providerOptions,
  networkOptions,
  onQueueStatusChange,
  onQueueMinAgeSecondsChange,
  onQueueSellerRefChange,
  onQueueProviderChange,
  onQueueNetworkChange,
  onRefreshQueue,
  onSelectSettlement,
  onSelectSeller,
  onSubmitReconciliationAction
}: {
  overview: OpsOverview | null;
  settlementDetail: SettlementDetailResponse | null;
  settlementLoadState: LoadState;
  selectedSettlementId: number | null;
  reconciliationQueue: ReconciliationQueueResponse | null;
  reconciliationLoadState: LoadState;
  queueStatus: string;
  queueMinAgeSeconds: number;
  queueSellerRef: string;
  queueProvider: string;
  queueNetwork: string;
  reconciliationAction: string | null;
  currentOperatorKeys: string[];
  providerOptions: string[];
  networkOptions: string[];
  onQueueStatusChange: (value: string) => void;
  onQueueMinAgeSecondsChange: (value: number) => void;
  onQueueSellerRefChange: (value: string) => void;
  onQueueProviderChange: (value: string) => void;
  onQueueNetworkChange: (value: string) => void;
  onRefreshQueue: () => Promise<void>;
  onSelectSettlement: (recordId: number) => Promise<void>;
  onSelectSeller: (sellerRef: string) => void;
  onSubmitReconciliationAction: (
    recordId: number,
    action: ReconciliationAction,
    reason: string
  ) => Promise<void>;
}) {
  return (
    <>
      <section className="summaryGrid compact" aria-label="Settlement summary">
        <Metric label="Records" value={formatNumber(overview?.settlement.records)} icon={<Database size={18} />} />
        <Metric label="Submitted" value={formatNumber(overview?.settlement.submitted)} icon={<WalletCards size={18} />} />
        <Metric label="Unknown" value={formatNumber(overview?.settlement.unknown)} tone={(overview?.settlement.unknown ?? 0) > 0 ? "warn" : "neutral"} icon={<AlertTriangle size={18} />} />
        <Metric label="Manual Review" value={formatNumber(overview?.settlement.manualReviewBacklog)} tone={(overview?.settlement.manualReviewBacklog ?? 0) > 0 ? "warn" : "neutral"} icon={<Siren size={18} />} />
      </section>
      <ReconciliationQueuePanel
        queue={reconciliationQueue}
        loadState={reconciliationLoadState}
        selectedRecordId={selectedSettlementId}
        status={queueStatus}
        minAgeSeconds={queueMinAgeSeconds}
        sellerRef={queueSellerRef}
        provider={queueProvider}
        network={queueNetwork}
        providerOptions={providerOptions}
        networkOptions={networkOptions}
        onStatusChange={onQueueStatusChange}
        onMinAgeSecondsChange={onQueueMinAgeSecondsChange}
        onSellerRefChange={onQueueSellerRefChange}
        onProviderChange={onQueueProviderChange}
        onNetworkChange={onQueueNetworkChange}
        onRefresh={onRefreshQueue}
        onSelectRecord={onSelectSettlement}
      />
      <SettlementPanel
        overview={overview}
        settlementDetail={settlementDetail}
        settlementLoadState={settlementLoadState}
        selectedSettlementId={selectedSettlementId}
        reconciliationAction={reconciliationAction}
        currentOperatorKeys={currentOperatorKeys}
        onSelectSettlement={onSelectSettlement}
        onSelectSeller={onSelectSeller}
        onSubmitReconciliationAction={onSubmitReconciliationAction}
      />
    </>
  );
}

function ControlsView({
  overview,
  targetType,
  target,
  paused,
  reason,
  submitting,
  canWrite,
  providerOptions,
  networkOptions,
  onChooseTargetType,
  onTargetChange,
  onPausedChange,
  onReasonChange,
  onSubmit
}: {
  overview: OpsOverview | null;
  targetType: PauseTargetType;
  target: string;
  paused: boolean;
  reason: string;
  submitting: boolean;
  canWrite: boolean;
  providerOptions: string[];
  networkOptions: string[];
  onChooseTargetType: (type: PauseTargetType) => void;
  onTargetChange: (value: string) => void;
  onPausedChange: (value: boolean) => void;
  onReasonChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>;
}) {
  return (
    <section className="workGrid controlsGrid">
      <div className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Emergency controls</p>
            <h2>Pause State</h2>
          </div>
          <PauseCircle size={20} aria-hidden="true" />
        </div>
        <PauseStateView overview={overview} />
      </div>
      <div className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Audited write</p>
            <h2>Apply Change</h2>
          </div>
          <Ban size={20} aria-hidden="true" />
        </div>
        <form className="pauseForm" onSubmit={onSubmit}>
          <div className="segmented" role="group" aria-label="Pause target type">
            {TARGET_TYPES.map((type) => (
              <button
                key={type}
                type="button"
                className={targetType === type ? "selected" : ""}
                onClick={() => onChooseTargetType(type)}
              >
                {type}
              </button>
            ))}
          </div>
          {targetType !== "global" ? (
            <label className="field">
              <span>Target</span>
              <input
                value={target}
                list={targetType === "provider" ? "provider-options" : "network-options"}
                onChange={(event) => onTargetChange(event.target.value)}
                required
              />
            </label>
          ) : null}
          <datalist id="provider-options">
            {providerOptions.map((provider) => (
              <option key={provider} value={provider} />
            ))}
          </datalist>
          <datalist id="network-options">
            {networkOptions.map((network) => (
              <option key={network} value={network} />
            ))}
          </datalist>
          <label className="field">
            <span>Reason</span>
            <input value={reason} onChange={(event) => onReasonChange(event.target.value)} minLength={3} maxLength={240} required />
          </label>
          <div className="formActions">
            <label className="switch">
              <input type="checkbox" checked={paused} onChange={(event) => onPausedChange(event.target.checked)} />
              <span>{paused ? "Pause" : "Resume"}</span>
            </label>
            <button className="primaryButton" type="submit" disabled={!canWrite || submitting}>
              {paused ? <Ban size={17} /> : <Undo2 size={17} />}
              {submitting ? "Submitting" : paused ? "Apply pause" : "Resume"}
            </button>
          </div>
          {!canWrite ? <p className="inlineWarning">Writes are disabled by the current backend state.</p> : null}
        </form>
      </div>
    </section>
  );
}

function AuditView({ overview }: { overview: OpsOverview | null }) {
  return (
    <section className="widePanel">
      <div className="panelHeader">
        <div>
          <p className="eyebrow">Audit</p>
          <h2>Recent Control Changes</h2>
        </div>
        <History size={20} aria-hidden="true" />
      </div>
      <AuditTable events={overview?.auditTail ?? []} />
    </section>
  );
}

function SecurityView({ overview, session }: { overview: OpsOverview | null; session: OpsSession }) {
  return (
    <section className="workGrid">
      <div className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Session</p>
            <h2>Authenticated Operator</h2>
          </div>
          <ShieldCheck size={20} aria-hidden="true" />
        </div>
        <div className="statusList">
          <div className="statusRow"><span>Email</span><strong>{session.email ?? "not provided"}</strong></div>
          <div className="statusRow"><span>Subject</span><strong>{session.subject ?? "unknown"}</strong></div>
          <div className="statusRow"><span>Expires</span><strong>{session.expiresAt ? formatUnixTime(session.expiresAt) : "unknown"}</strong></div>
        </div>
      </div>
      <div className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Authorization</p>
            <h2>Backend Enforcement</h2>
          </div>
          <Lock size={20} aria-hidden="true" />
        </div>
        <div className="statusList">
          <div className="statusRow"><span>OIDC</span><strong>required</strong></div>
          <div className="statusRow"><span>OpenFGA</span><strong>enforced</strong></div>
          <div className="statusRow"><span>Control plane</span><strong>{overview?.components.controlPlane.status ?? "loading"}</strong></div>
        </div>
      </div>
    </section>
  );
}

function SummaryMetrics({
  overview,
  activePauseCount
}: {
  overview: OpsOverview | null;
  activePauseCount: number;
}) {
  return (
    <section className="summaryGrid" aria-label="Operations summary">
      <Metric label="Records" value={formatNumber(overview?.settlement.records)} icon={<Database size={18} />} />
      <Metric label="Submitted" value={formatNumber(overview?.settlement.submitted)} icon={<WalletCards size={18} />} />
      <Metric label="Manual Review" value={formatNumber(overview?.settlement.manualReviewBacklog)} tone={(overview?.settlement.manualReviewBacklog ?? 0) > 0 ? "warn" : "neutral"} icon={<Siren size={18} />} />
      <Metric label="Active Pauses" value={formatNumber(activePauseCount)} tone={activePauseCount > 0 ? "warn" : "neutral"} icon={<Ban size={18} />} />
    </section>
  );
}

function HealthPanel({ overview }: { overview: OpsOverview | null }) {
  return (
    <div className="panel">
      <div className="panelHeader">
        <div>
          <p className="eyebrow">Health</p>
          <h2>Components</h2>
        </div>
        <Gauge size={20} aria-hidden="true" />
      </div>
      <div className="healthList">
        {overview ? (
          Object.entries(overview.components).map(([key, health]) => (
            <HealthRow key={key} health={health} />
          ))
        ) : (
          <SkeletonRows count={5} />
        )}
      </div>
    </div>
  );
}

function ReconciliationQueuePanel({
  queue,
  loadState,
  selectedRecordId,
  status,
  minAgeSeconds,
  sellerRef,
  provider,
  network,
  providerOptions,
  networkOptions,
  onStatusChange,
  onMinAgeSecondsChange,
  onSellerRefChange,
  onProviderChange,
  onNetworkChange,
  onRefresh,
  onSelectRecord
}: {
  queue: ReconciliationQueueResponse | null;
  loadState: LoadState;
  selectedRecordId: number | null;
  status: string;
  minAgeSeconds: number;
  sellerRef: string;
  provider: string;
  network: string;
  providerOptions: string[];
  networkOptions: string[];
  onStatusChange: (value: string) => void;
  onMinAgeSecondsChange: (value: number) => void;
  onSellerRefChange: (value: string) => void;
  onProviderChange: (value: string) => void;
  onNetworkChange: (value: string) => void;
  onRefresh: () => Promise<void>;
  onSelectRecord: (recordId: number) => Promise<void>;
}) {
  const counts = queue?.counts ?? {};
  const loading = loadState === "loading" && !queue;
  const failed = loadState === "error" && !queue;
  return (
    <section className="widePanel reconciliationPanel">
      <div className="panelHeader">
        <div>
          <p className="eyebrow">Reconciliation queue</p>
          <h2>Operational Triage</h2>
        </div>
        <button
          className="iconButton"
          type="button"
          onClick={() => void onRefresh()}
          aria-label="Refresh reconciliation queue"
          aria-busy={loadState === "loading"}
        >
          <RefreshCw size={17} />
        </button>
      </div>
      <section className="queueMetricGrid" aria-label="Reconciliation queue summary">
        <QueueMetric label="Unknown" value={counts.unknown ?? 0} tone={(counts.unknown ?? 0) > 0 ? "warn" : "neutral"} />
        <QueueMetric label="Submitted" value={counts.submitted ?? 0} tone={(counts.submitted ?? 0) > 0 ? "warn" : "neutral"} />
        <QueueMetric label="In Progress" value={counts.settle_in_progress ?? 0} tone={(counts.settle_in_progress ?? 0) > 0 ? "warn" : "neutral"} />
        <QueueMetric label="Manual Review" value={counts.manual_review ?? 0} tone={(counts.manual_review ?? 0) > 0 ? "warn" : "neutral"} />
      </section>
      <div className="queueFilters" aria-label="Reconciliation queue filters">
        <label className="field compactField">
          <span>Status</span>
          <select value={status} onChange={(event) => onStatusChange(event.target.value)}>
            <option value="all">All actionable</option>
            <option value="unknown">Unknown</option>
            <option value="submitted">Submitted</option>
            <option value="settle_in_progress">In progress</option>
            <option value="manual_review">Manual review</option>
          </select>
        </label>
        <label className="field compactField">
          <span>Minimum Age</span>
          <select value={minAgeSeconds} onChange={(event) => onMinAgeSecondsChange(Number(event.target.value))}>
            <option value={0}>Any age</option>
            <option value={300}>5 minutes</option>
            <option value={900}>15 minutes</option>
            <option value={3600}>1 hour</option>
          </select>
        </label>
        <label className="field compactField">
          <span>Seller</span>
          <input value={sellerRef} onChange={(event) => onSellerRefChange(event.target.value)} placeholder="seller ref" />
        </label>
        <label className="field compactField">
          <span>Provider</span>
          <input value={provider} list="queue-provider-options" onChange={(event) => onProviderChange(event.target.value)} placeholder="any" />
        </label>
        <label className="field compactField">
          <span>Network</span>
          <input value={network} list="queue-network-options" onChange={(event) => onNetworkChange(event.target.value)} placeholder="any" />
        </label>
      </div>
      <datalist id="queue-provider-options">
        {providerOptions.map((option) => (
          <option key={option} value={option} />
        ))}
      </datalist>
      <datalist id="queue-network-options">
        {networkOptions.map((option) => (
          <option key={option} value={option} />
        ))}
      </datalist>
      {loading ? (
        <SkeletonRows count={5} />
      ) : failed ? (
        <div className="emptyState compact errorState">
          <AlertTriangle size={22} />
          <p>Reconciliation queue unavailable.</p>
        </div>
      ) : queue?.items.length ? (
        <ReconciliationQueueTable
          records={queue.items}
          selectedRecordId={selectedRecordId}
          onSelectRecord={onSelectRecord}
        />
      ) : (
        <div className="emptyState compact">
          <CheckCircle2 size={22} />
          <p>No matching reconciliation records.</p>
        </div>
      )}
      {queue?.truncated ? (
        <div className="queueWarning" role="status">
          <AlertTriangle size={16} />
          <span>Queue is truncated at {formatNumber(queue.filters.limit)} records. Narrow the filters before taking action.</span>
        </div>
      ) : null}
    </section>
  );
}

function QueueMetric({
  label,
  value,
  tone
}: {
  label: string;
  value: number;
  tone: "warn" | "neutral";
}) {
  return (
    <div className={`queueMetric ${tone}`}>
      <span>{label}</span>
      <strong className="mono">{formatNumber(value)}</strong>
    </div>
  );
}

function ReconciliationQueueTable({
  records,
  selectedRecordId,
  onSelectRecord
}: {
  records: ReconciliationQueueResponse["items"];
  selectedRecordId: number | null;
  onSelectRecord: (recordId: number) => Promise<void>;
}) {
  const handleKeyboardSelect = (
    event: KeyboardEvent<HTMLTableRowElement>,
    recordId: number
  ) => {
    if (event.target !== event.currentTarget) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      void onSelectRecord(recordId);
    }
  };
  return (
    <div className="tableWrap queueTableWrap">
      <table className="queueTable">
        <thead>
          <tr>
            <th>Age</th>
            <th>Status</th>
            <th>Seller</th>
            <th>Rail</th>
            <th>Amount</th>
            <th>Attempts</th>
            <th>Owner</th>
            <th>Open</th>
          </tr>
        </thead>
        <tbody>
          {records.map((record) => (
            <tr
              key={record.recordId}
              className={selectedRecordId === record.recordId ? "selectedRow" : ""}
              role="button"
              tabIndex={0}
              aria-pressed={selectedRecordId === record.recordId}
              aria-label={`Open reconciliation record ${record.recordId}`}
              onClick={() => void onSelectRecord(record.recordId)}
              onKeyDown={(event) => handleKeyboardSelect(event, record.recordId)}
            >
              <td className="mono">{formatDuration(record.ageSeconds)}</td>
              <td>
                <span className={`statusBadge ${settlementStatusTone(record.status)}`}>
                  {labelize(record.status)}
                </span>
                {record.errorReason ? <span className="cellMeta">{record.errorReason}</span> : null}
              </td>
              <td>
                <strong>{record.sellerRef}</strong>
                <span className="cellMeta mono">{record.paymentProfileId}</span>
              </td>
              <td>
                <strong>{settlementRailLabel(record)}</strong>
                <span className="cellMeta">{networkLabel(record.network)}</span>
              </td>
              <td className="mono">{record.amountUsdc ? `${record.amountUsdc} USDC` : record.amountAtomic || "unknown"}</td>
              <td className="mono">
                {formatNumber(record.reconciliation.attempts)}
                {record.reconciliation.duplicateClaims ? ` / ${formatNumber(record.reconciliation.duplicateClaims)} dup` : ""}
              </td>
              <td className="mono">{record.reconciliation.owner || "unclaimed"}</td>
              <td>
                <button
                  className="iconButton smallIconButton"
                  type="button"
                  aria-label={`Open reconciliation record ${record.recordId}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    void onSelectRecord(record.recordId);
                  }}
                >
                  <Eye size={15} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SettlementPanel({
  overview,
  settlementDetail,
  settlementLoadState,
  selectedSettlementId,
  reconciliationAction,
  currentOperatorKeys,
  onSelectSettlement,
  onSelectSeller,
  onSubmitReconciliationAction
}: {
  overview: OpsOverview | null;
  settlementDetail: SettlementDetailResponse | null;
  settlementLoadState: LoadState;
  selectedSettlementId: number | null;
  reconciliationAction: string | null;
  currentOperatorKeys: string[];
  onSelectSettlement: (recordId: number) => Promise<void>;
  onSelectSeller: (sellerRef: string) => void;
  onSubmitReconciliationAction: (
    recordId: number,
    action: ReconciliationAction,
    reason: string
  ) => Promise<void>;
}) {
  return (
    <div className="workGrid">
      <SettlementDetailPanel
        detail={settlementDetail}
        loadState={settlementLoadState}
        selectedRecordId={selectedSettlementId}
        reconciliationAction={reconciliationAction}
        currentOperatorKeys={currentOperatorKeys}
        onSelectSeller={onSelectSeller}
        onSubmitReconciliationAction={onSubmitReconciliationAction}
      />
      <section className="widePanel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Money movement</p>
            <h2>Recent Settlements</h2>
          </div>
          <Database size={20} aria-hidden="true" />
        </div>
        <RecentSettlementsTable
          records={overview?.settlement.recentRecords ?? []}
          loading={!overview}
          selectedRecordId={selectedSettlementId}
          onSelectRecord={onSelectSettlement}
          onSelectSeller={onSelectSeller}
        />
      </section>
      <section className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Settlement lifecycle</p>
            <h2>Status Counts</h2>
          </div>
          <WalletCards size={20} aria-hidden="true" />
        </div>
        <StatusCounts overview={overview} />
      </section>
      <section className="panel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Reconciliation age</p>
            <h2>Oldest Records</h2>
          </div>
          <Activity size={20} aria-hidden="true" />
        </div>
        <OldestAgeRows overview={overview} />
      </section>
      <section className="widePanel">
        <div className="panelHeader">
          <div>
            <p className="eyebrow">Provider status</p>
            <h2>Provider / Network Counts</h2>
          </div>
          <Route size={20} aria-hidden="true" />
        </div>
        <ProviderNetworkCounts overview={overview} />
      </section>
    </div>
  );
}

function RecentSettlementsTable({
  records,
  loading,
  selectedRecordId,
  onSelectRecord,
  onSelectSeller
}: {
  records: SettlementRecordSummary[];
  loading: boolean;
  selectedRecordId: number | null;
  onSelectRecord: (recordId: number) => Promise<void>;
  onSelectSeller: (sellerRef: string) => void;
}) {
  if (loading) {
    return <SkeletonRows count={6} />;
  }
  if (records.length === 0) {
    return <p className="empty">No settlement records.</p>;
  }
  const handleKeyboardSelect = (
    event: KeyboardEvent<HTMLTableRowElement>,
    recordId: number
  ) => {
    if (event.target !== event.currentTarget) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      void onSelectRecord(recordId);
    }
  };
  return (
    <div className="tableWrap settlementTableWrap">
      <table className="settlementTable">
        <thead>
          <tr>
            <th>Time</th>
            <th>Status</th>
            <th>Seller</th>
            <th>Rail</th>
            <th>Amount</th>
            <th>Payer</th>
            <th>Pay To</th>
            <th>Transaction</th>
            <th>Open</th>
          </tr>
        </thead>
        <tbody>
          {records.map((record) => (
            <tr
              key={record.recordId}
              className={selectedRecordId === record.recordId ? "selectedRow" : ""}
              role="button"
              tabIndex={0}
              aria-pressed={selectedRecordId === record.recordId}
              aria-label={`Open settlement record ${record.recordId}`}
              onClick={() => void onSelectRecord(record.recordId)}
              onKeyDown={(event) => handleKeyboardSelect(event, record.recordId)}
            >
              <td className="mono">{record.updatedAt ? formatTime(record.updatedAt) : "unknown"}</td>
              <td>
                <span className={`statusBadge ${settlementStatusTone(record.status)}`}>
                  {labelize(record.status)}
                </span>
              </td>
              <td>
                <button
                  className="linkButton mono"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    onSelectSeller(record.sellerRef);
                  }}
                >
                  {record.sellerRef}
                </button>
                <span className="cellMeta mono">{record.paymentProfileId}</span>
              </td>
              <td>
                <strong>{settlementRailLabel(record)}</strong>
                <span className="cellMeta">{networkLabel(record.network)}</span>
              </td>
              <td className="mono">{record.amountUsdc ? `${record.amountUsdc} USDC` : record.amountAtomic || "unknown"}</td>
              <td className="mono">{shorten(record.payer)}</td>
              <td className="mono">{shorten(record.payTo)}</td>
              <td>
                <TransactionCell record={record} />
              </td>
              <td>
                <button
                  className="secondaryButton compactButton"
                  type="button"
                  aria-label={`View settlement ${record.recordId}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    void onSelectRecord(record.recordId);
                  }}
                >
                  <Eye size={15} />
                  Inspect
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TransactionCell({ record }: { record: SettlementRecordSummary }) {
  if (!record.transaction) {
    return <span className="mono">{record.errorReason || "pending"}</span>;
  }
  return (
    <span className="transactionCell">
      <span className="mono">{shorten(record.transaction)}</span>
      <span className="cellMeta">
        {record.provider === "circle_gateway" ? "Gateway reference" : "Evidence in detail"}
      </span>
    </span>
  );
}

function SettlementDetailPanel({
  detail,
  loadState,
  selectedRecordId,
  reconciliationAction,
  currentOperatorKeys,
  onSelectSeller,
  onSubmitReconciliationAction
}: {
  detail: SettlementDetailResponse | null;
  loadState: LoadState;
  selectedRecordId: number | null;
  reconciliationAction: string | null;
  currentOperatorKeys: string[];
  onSelectSeller: (sellerRef: string) => void;
  onSubmitReconciliationAction: (
    recordId: number,
    action: ReconciliationAction,
    reason: string
  ) => Promise<void>;
}) {
  const record = detail?.record;
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [copyingValue, setCopyingValue] = useState<string | null>(null);
  const [leaseClockMs, setLeaseClockMs] = useState(() => Date.now());
  const [actionReasonState, setActionReasonState] = useState<{
    recordId: number | null;
    value: string;
  }>({ recordId: null, value: DEFAULT_RECONCILIATION_REASON });
  useEffect(() => {
    const interval = window.setInterval(() => setLeaseClockMs(Date.now()), 30_000);
    return () => window.clearInterval(interval);
  }, []);
  const actionReason =
    actionReasonState.recordId === (record?.recordId ?? null)
      ? actionReasonState.value
      : DEFAULT_RECONCILIATION_REASON;
  const reasonReady = actionReason.trim().length >= 3;
  const leaseUntilMs = record?.reconciliation.leaseUntil
    ? Date.parse(record.reconciliation.leaseUntil)
    : Number.NaN;
  const leaseActive = Number.isFinite(leaseUntilMs) && leaseUntilMs > leaseClockMs;
  const ownedByCurrentOperator = Boolean(
    record?.reconciliation.owner && currentOperatorKeys.includes(record.reconciliation.owner)
  );
  const canClaim = !record?.reconciliation.owner || !leaseActive;
  const canRelease = Boolean(record) && ownedByCurrentOperator;
  const canMoveToManualReview = record?.status === "unknown" && ownedByCurrentOperator && leaseActive;
  const currentActionForRecord = selectedRecordId ? reconciliationAction?.endsWith(`:${selectedRecordId}`) : false;
  return (
    <section className="widePanel settlementDetailPanel">
      <div className="panelHeader">
        <div>
          <p className="eyebrow">Settlement detail</p>
          <h2>{selectedRecordId ? `Record ${selectedRecordId}` : "Select a Settlement"}</h2>
        </div>
        <Eye size={20} aria-hidden="true" />
      </div>
      {loadState === "loading" ? (
        <SkeletonRows count={6} />
      ) : record ? (
        <div className="detailGrid">
          <div className="statusList">
            <div className="statusRow"><span>Status</span><strong>{labelize(record.status)}</strong></div>
            {record.errorReason ? (
              <div className="statusRow"><span>Reason</span><strong className="mono">{record.errorReason}</strong></div>
            ) : null}
            <div className="statusRow"><span>Rail</span><strong>{settlementRailLabel(record)}</strong></div>
            <div className="statusRow"><span>Network</span><strong>{networkLabel(record.network)}</strong></div>
            <div className="statusRow"><span>Amount</span><strong className="mono">{record.amountUsdc ? `${record.amountUsdc} USDC` : record.amountAtomic || "unknown"}</strong></div>
            <div className="statusRow"><span>Updated</span><strong className="mono">{record.updatedAt ? formatTime(record.updatedAt) : "unknown"}</strong></div>
          </div>
          <div className="statusList">
            <div className="statusRow">
              <span>Seller</span>
              <button className="linkButton mono" type="button" onClick={() => onSelectSeller(record.sellerRef)}>
                {record.sellerRef}
              </button>
            </div>
            <div className="statusRow"><span>Payment Profile</span><strong className="mono">{record.paymentProfileId}</strong></div>
            <div className="statusRow"><span>Trace</span><strong className="mono">{record.traceId || "none"}</strong></div>
            <div className="statusRow"><span>Fingerprint</span><strong className="mono">{shorten(record.fingerprint)}</strong></div>
            <div className="statusRow"><span>Resource Hash</span><strong className="mono">{shorten(record.rawRequirements.resourceHash ?? "") || "redacted"}</strong></div>
          </div>
          <div className="wideDetailBlock">
            <div className="detailSectionHeader">
              <h3>Transaction Evidence</h3>
              <div className="detailActions">
                {record.transaction ? (
                  <button
                    className="secondaryButton compactButton"
                    type="button"
                    disabled={copyingValue === record.transaction}
                    aria-busy={copyingValue === record.transaction}
                    onClick={() => void copyText(record.transaction, setCopyStatus, setCopyingValue)}
                  >
                    <Copy size={15} />
                    Copy transaction
                  </button>
                ) : null}
                {record.explorerUrl ? (
                  <a className="secondaryButton compactButton" href={record.explorerUrl} target="_blank" rel="noreferrer">
                    <ExternalLink size={15} />
                    Open explorer
                  </a>
                ) : null}
              </div>
            </div>
            <div className="statusList">
              <div className="statusRow"><span>{record.transactionKind === "gateway_transfer" ? "Gateway Transfer" : "Transaction Hash"}</span><strong className="mono">{record.transaction || "pending"}</strong></div>
              <div className="statusRow"><span>Payer</span><strong className="mono">{record.payer || "unknown"}</strong></div>
              <div className="statusRow"><span>Pay To</span><strong className="mono">{record.payTo || "unknown"}</strong></div>
              <div className="statusRow"><span>Asset</span><strong className="mono">{assetLabel(record.asset)}</strong></div>
            </div>
            <div className="evidenceActions">
              {record.traceId ? (
                <button
                  className="secondaryButton compactButton"
                  type="button"
                  disabled={copyingValue === record.traceId}
                  aria-busy={copyingValue === record.traceId}
                  onClick={() => void copyText(record.traceId, setCopyStatus, setCopyingValue)}
                >
                  <Copy size={15} />
                  Copy trace
                </button>
              ) : null}
              {record.payer ? (
                <button
                  className="secondaryButton compactButton"
                  type="button"
                  disabled={copyingValue === record.payer}
                  aria-busy={copyingValue === record.payer}
                  onClick={() => void copyText(record.payer, setCopyStatus, setCopyingValue)}
                >
                  <Copy size={15} />
                  Copy payer
                </button>
              ) : null}
              {record.payTo ? (
                <button
                  className="secondaryButton compactButton"
                  type="button"
                  disabled={copyingValue === record.payTo}
                  aria-busy={copyingValue === record.payTo}
                  onClick={() => void copyText(record.payTo, setCopyStatus, setCopyingValue)}
                >
                  <Copy size={15} />
                  Copy pay to
                </button>
              ) : null}
              {copyStatus ? <span className="inlineHint">{copyStatus}</span> : null}
            </div>
          </div>
          <div className="wideDetailBlock">
            <div className="detailSectionHeader">
              <h3>Settlement Attempts</h3>
            </div>
            {detail.attempts.length ? (
              <div className="tableWrap">
                <table>
                  <thead>
                    <tr>
                      <th>Attempt</th>
                      <th>Status</th>
                      <th>Started</th>
                      <th>Finished</th>
                      <th>Transaction</th>
                      <th>Open</th>
                      <th>Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.attempts.map((attempt) => (
                      <tr key={attempt.attemptId}>
                        <td className="mono">{attempt.attemptId}</td>
                        <td>{labelize(attempt.status)}</td>
                        <td className="mono">{attempt.startedAt ? formatTime(attempt.startedAt) : "unknown"}</td>
                        <td className="mono">{attempt.finishedAt ? formatTime(attempt.finishedAt) : "pending"}</td>
                        <td className="mono">{attempt.transaction || "none"}</td>
                        <td>
                          {attempt.explorerUrl ? (
                            <a className="iconButton smallIconButton" href={attempt.explorerUrl} target="_blank" rel="noreferrer" aria-label={`Open attempt ${attempt.attemptId} transaction`}>
                              <ExternalLink size={15} />
                            </a>
                          ) : (
                            <span className="cellMeta">{attempt.transactionKind === "gateway_transfer" ? "Gateway reference" : attempt.transaction ? "Reference" : "none"}</span>
                          )}
                        </td>
                        <td>{attempt.errorReason || "none"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty">No settlement attempts recorded.</p>
            )}
          </div>
          <div className="wideDetailBlock">
            <div className="detailSectionHeader">
              <h3>Reconciliation</h3>
            </div>
            <div className="statusList">
              <div className="statusRow"><span>Attempts</span><strong className="mono">{formatNumber(record.reconciliation.attempts)}</strong></div>
              <div className="statusRow"><span>Lease Owner</span><strong className="mono">{record.reconciliation.owner || "none"}</strong></div>
              <div className="statusRow"><span>Lease Until</span><strong className="mono">{record.reconciliation.leaseUntil ? formatTime(record.reconciliation.leaseUntil) : "none"}</strong></div>
              <div className="statusRow"><span>Duplicate Claims</span><strong className="mono">{formatNumber(record.reconciliation.duplicateClaims)}</strong></div>
              <div className="statusRow"><span>Last Duplicate</span><strong className="mono">{record.reconciliation.lastDuplicateAt ? formatTime(record.reconciliation.lastDuplicateAt) : "none"}</strong></div>
            </div>
            <div className="reconciliationActions">
              <label className="field compactField">
                <span>Audit Reason</span>
                <input
                  value={actionReason}
                  onChange={(event) =>
                    setActionReasonState({
                      recordId: record.recordId,
                      value: event.target.value
                    })
                  }
                  minLength={3}
                  maxLength={240}
                  required
                />
              </label>
              <div className="actionButtonRow">
                <button
                  className="secondaryButton compactButton"
                  type="button"
                  disabled={!reasonReady || currentActionForRecord || !canClaim}
                  aria-busy={reconciliationAction === `claim:${record.recordId}`}
                  onClick={() => void onSubmitReconciliationAction(record.recordId, "claim", actionReason)}
                >
                  {reconciliationAction === `claim:${record.recordId}` ? "Claiming" : "Claim"}
                </button>
                <button
                  className="secondaryButton compactButton"
                  type="button"
                  disabled={!reasonReady || currentActionForRecord || !canRelease}
                  aria-busy={reconciliationAction === `release:${record.recordId}`}
                  onClick={() => void onSubmitReconciliationAction(record.recordId, "release", actionReason)}
                >
                  {reconciliationAction === `release:${record.recordId}` ? "Releasing" : "Release"}
                </button>
                <button
                  className="primaryButton compactButton"
                  type="button"
                  disabled={!reasonReady || currentActionForRecord || !canMoveToManualReview}
                  aria-busy={reconciliationAction === `manual-review:${record.recordId}`}
                  onClick={() => void onSubmitReconciliationAction(record.recordId, "manual-review", actionReason)}
                >
                  {reconciliationAction === `manual-review:${record.recordId}` ? "Moving" : "Move to manual review"}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className="emptyState compact">
          <Eye size={22} />
          <p>Select a settlement to inspect transaction evidence, attempts, and reconciliation state.</p>
        </div>
      )}
    </section>
  );
}

function AuthGate({ state, error }: { state: AuthState; error: string | null }) {
  return (
    <main className="authShell">
      <section className="authPanel" aria-labelledby="auth-title">
        <div className="authBrand">
          <span className="brandMark">
            <ShieldCheck size={24} />
          </span>
          <div>
            <p className="eyebrow">OmniClaw facilitator</p>
            <h1 id="auth-title">Control Plane</h1>
          </div>
        </div>
        <div className="authBody">
          <KeyRound size={30} aria-hidden="true" />
          <h2>Sign in to continue</h2>
          <p>Access is restricted to authorized OmniClaw operators. Sign-in uses OIDC; permissions are checked server-side with OpenFGA.</p>
          {error ? (
            <div className="authError" role="alert">
              <AlertTriangle size={17} />
              <span>{error}</span>
            </div>
          ) : null}
          <div className="authActions">
            <a className="authPrimary" href="/api/auth/login">
              <LogIn size={18} />
              Sign in
            </a>
          </div>
        </div>
        <div className="authFooter">
          <span>{state === "checking" ? "Checking session" : "OIDC required"}</span>
          <span>OpenFGA enforced</span>
        </div>
      </section>
    </main>
  );
}

function RailCards({ overview }: { overview: OpsOverview | null }) {
  return (
    <section className="railGrid" id="rails" aria-label="Control plane readiness">
      <RailCard
        icon={<Route size={18} />}
        title="Exact EVM Provider"
        description={`${overview?.providers.providerCount ?? 0} configured provider${overview?.providers.providerCount === 1 ? "" : "s"}. Arc is one network under the provider-neutral EVM exact rail.`}
        badge={overview?.providers.names[0] ?? "exact_evm"}
        tone="neutral"
      />
      <RailCard
        icon={<ShieldCheck size={18} />}
        title="OIDC + OpenFGA"
        description="Operator identity is issued by OIDC and every write is authorized by relation checks."
        badge="enforced"
        tone="green"
      />
      <RailCard
        icon={<WalletCards size={18} />}
        title="Reconciliation"
        description="Settlement records, attempts, manual review backlog, and control-plane audit are backed by Postgres."
        badge={`${formatNumber(overview?.settlement.manualReviewBacklog)} review`}
        tone={(overview?.settlement.manualReviewBacklog ?? 0) > 0 ? "yellow" : "green"}
      />
      <RailCard
        icon={<CircuitBoard size={18} />}
        title="Rate Limit + Redis"
        description={`Control-plane writes are ${overview?.pauseState.writesEnabled ? "enabled" : "disabled"} and rate-limited through the hosted backend.`}
        badge={overview?.pauseState.writesEnabled ? "writes enabled" : "writes disabled"}
        tone={overview?.pauseState.writesEnabled ? "neutral" : "yellow"}
      />
    </section>
  );
}

function RailCard({
  icon,
  title,
  description,
  badge,
  tone
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  badge: string;
  tone: "neutral" | "green" | "yellow";
}) {
  return (
    <div className="railCard">
      <div className="railIcon">{icon}</div>
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
        <span className={`badge ${tone === "green" ? "badgeGreen" : tone === "yellow" ? "badgeYellow" : "badgeNeutral"}`}>{badge}</span>
      </div>
    </div>
  );
}

function StatusNotice({ error }: { error: OpsApiError }) {
  const isAuth = error.status === 401 || error.status === 403;
  return (
    <div className="notice" role="alert">
      {isAuth ? <Lock size={18} /> : <AlertTriangle size={18} />}
      <span>{error.status}: {error.detail}</span>
    </div>
  );
}

function Metric({
  label,
  value,
  icon,
  tone = "neutral"
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  tone?: "neutral" | "good" | "warn";
}) {
  return (
    <div className={`metric ${tone}`}>
      <div className="metricIcon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function HealthRow({ health }: { health: ComponentHealth }) {
  const ok = health.status === "ok";
  return (
    <div className="healthRow">
      <span className={ok ? "dot good" : "dot warn"} />
      <span>{labelize(health.name)}</span>
      <strong>{health.errorType ?? health.status}</strong>
    </div>
  );
}

function PauseStateView({ overview }: { overview: OpsOverview | null }) {
  const state = overview?.pauseState;
  if (!state) {
    return <SkeletonRows count={3} />;
  }
  return (
    <div className="pauseState">
      <StatePill label="Global" active={state.globalPaused} />
      <StatePill label="Providers" active={state.providers.length > 0} value={state.providers.join(", ") || "none"} />
      <StatePill label="Networks" active={state.networks.length > 0} value={state.networks.join(", ") || "none"} />
    </div>
  );
}

function StatePill({ label, active, value }: { label: string; active: boolean; value?: string }) {
  return (
    <div className={`statePill ${active ? "active" : ""}`}>
      {active ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
      <span>{label}</span>
      <strong>{value ?? (active ? "paused" : "clear")}</strong>
    </div>
  );
}

function StatusCounts({ overview }: { overview: OpsOverview | null }) {
  const counts = useMemo(() => Object.entries(overview?.settlement.statusCounts ?? {}), [overview]);
  if (!overview) {
    return <SkeletonRows count={4} />;
  }
  if (counts.length === 0) {
    return <p className="empty">No settlement records.</p>;
  }
  return (
    <div className="statusList">
      {counts.map(([status, count]) => (
        <div className="statusRow" key={status}>
          <span>{labelize(status)}</span>
          <strong>{formatNumber(count)}</strong>
        </div>
      ))}
    </div>
  );
}

function OldestAgeRows({ overview }: { overview: OpsOverview | null }) {
  const ages = useMemo(() => Object.entries(overview?.settlement.oldestAgeSeconds ?? {}), [overview]);
  if (!overview) {
    return <SkeletonRows count={4} />;
  }
  if (ages.length === 0) {
    return <p className="empty">No stale settlement records.</p>;
  }
  return (
    <div className="statusList">
      {ages.map(([status, seconds]) => (
        <div className="statusRow" key={status}>
          <span>{labelize(status)}</span>
          <strong>{formatDuration(seconds)}</strong>
        </div>
      ))}
    </div>
  );
}

function ProviderNetworkCounts({ overview }: { overview: OpsOverview | null }) {
  const rows = overview?.settlement.byProviderNetwork ?? [];
  if (!overview) {
    return <SkeletonRows count={4} />;
  }
  if (rows.length === 0) {
    return <p className="empty">No provider settlement records.</p>;
  }
  return (
    <div className="tableWrap">
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th>Network</th>
            <th>Status</th>
            <th>Count</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.provider}-${row.network}-${row.status}`}>
              <td className="mono">{row.provider}</td>
              <td className="mono">{row.network ? networkLabel(row.network) : "none"}</td>
              <td>{labelize(row.status)}</td>
              <td className="mono">{formatNumber(row.count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AuditTable({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="empty">No control-plane audit events.</p>;
  }
  return (
    <div className="tableWrap">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Action</th>
            <th>Target</th>
            <th>State</th>
            <th>Actor</th>
            <th>Correlation</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={`${event.eventId}-${event.correlationId}`}>
              <td className="mono">{formatTime(event.createdAt)}</td>
              <td>{auditActionLabel(event.action)}</td>
              <td className="mono">{auditTargetLabel(event.targetType)}:{event.target}</td>
              <td>{auditStateText(event)}</td>
              <td className="mono">{event.actor}</td>
              <td className="mono">{event.correlationId}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SkeletonRows({ count }: { count: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, index) => (
        <div className="skeleton" key={index} />
      ))}
    </>
  );
}

async function copyText(
  value: string,
  onStatus: (value: string | null) => void,
  onCopyingValue: (value: string | null) => void
): Promise<void> {
  onCopyingValue(value);
  if (typeof navigator === "undefined" || !navigator.clipboard) {
    onStatus("Clipboard unavailable");
    onCopyingValue(null);
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    onStatus("Copied");
    window.setTimeout(() => onStatus(null), 2_500);
  } catch {
    onStatus("Copy failed");
  } finally {
    onCopyingValue(null);
  }
}

function paymentNetworkFor(networkId: string): (typeof PAYMENT_NETWORKS)[number] {
  return PAYMENT_NETWORKS.find((network) => network.id === networkId) ?? PAYMENT_NETWORKS[0];
}

function networkLabel(networkId: string): string {
  return PAYMENT_NETWORKS.find((network) => network.id === networkId)?.label ?? networkId;
}

function assetLabel(asset: string): string {
  if (asset.toLowerCase() === DEFAULT_ASSET.toLowerCase()) {
    return "USDC";
  }
  return asset || "unknown";
}

function signerProviderLabel(provider: string): string {
  if (provider === "exact_evm") {
    return "Exact EVM signer";
  }
  return labelize(provider);
}

function settlementRailLabel(record: SettlementRecordSummary): string {
  if (record.provider === "circle_gateway" || record.rail === "GatewayWalletBatched") {
    return "Gateway batch";
  }
  if (record.provider === "exact_evm") {
    return "Exact EVM";
  }
  return labelize(record.provider || record.rail || "unknown");
}

function settlementStatusTone(status: string): "good" | "warn" | "bad" | "neutral" {
  if (status === "settled" || status === "reconciled") {
    return "good";
  }
  if (status === "unknown" || status === "submitted" || status === "manual_review") {
    return "warn";
  }
  if (status === "settle_failed") {
    return "bad";
  }
  return "neutral";
}

function shorten(value: string): string {
  if (!value) {
    return "";
  }
  if (value.length <= 18) {
    return value;
  }
  return `${value.slice(0, 10)}...${value.slice(-6)}`;
}

function deriveSellerReference(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "-")
    .replace(/^[^a-z0-9]+/g, "")
    .replace(/[-_.]+$/g, "");
  const base = slug || "seller";
  const suffix = Date.now().toString(36);
  return `${base.slice(0, 63 - suffix.length)}-${suffix}`;
}

function auditStateText(event: AuditEvent): string {
  if (event.action === "seller_create") {
    return event.reason.startsWith("api_key_prefix:")
      ? `created, ${event.reason.replace("api_key_prefix:", "key ")}`
      : "created";
  }
  if (event.action === "seller_api_key_issue") {
    return event.reason.startsWith("api_key_prefix:")
      ? `issued, ${event.reason.replace("api_key_prefix:", "key ")}`
      : "issued";
  }
  if (event.action === "seller_api_key_revoke") {
    return event.reason.startsWith("api_key_prefix:")
      ? `revoked, ${event.reason.replace("api_key_prefix:", "key ")}`
      : "revoked";
  }
  if (event.action === "reconciliation_claim") {
    return "claimed";
  }
  if (event.action === "reconciliation_release") {
    return "released";
  }
  if (event.action === "reconciliation_manual_review") {
    return "moved to manual review";
  }
  return `${event.before ? "paused" : "clear"} > ${event.after ? "paused" : "clear"}`;
}

function auditActionLabel(action: string): string {
  if (action === "seller_create") {
    return "Seller Create";
  }
  if (action === "seller_api_key_issue") {
    return "API Key Issue";
  }
  if (action === "seller_api_key_revoke") {
    return "API Key Revoke";
  }
  if (action === "pause_set" || action === "pause set") {
    return "Pause Set";
  }
  return labelize(action);
}

function auditTargetLabel(targetType: string): string {
  return targetType;
}

function normalizeError(caught: unknown): OpsApiError {
  if (
    typeof caught === "object" &&
    caught !== null &&
    "status" in caught &&
    typeof caught.status === "number"
  ) {
    return caught as OpsApiError;
  }
  return { status: 500, detail: "Unexpected ops console error" };
}

function formatNumber(value: number | undefined): string {
  return new Intl.NumberFormat("en-US").format(value ?? 0);
}

function formatWei(value: number | undefined): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value ?? 0);
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    month: "short",
    day: "numeric"
  }).format(date);
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "0s";
  }
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) {
    return `${days}d ${hours}h`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m`;
  }
  return `${Math.floor(seconds)}s`;
}

function formatUnixTime(value: number): string {
  return formatTime(new Date(value * 1000).toISOString());
}

function labelize(value: string): string {
  return value.replaceAll("_", " ");
}

function initials(value: string | undefined): string {
  if (!value) {
    return "OC";
  }
  const [first, second] = value
    .replace(/@.*/, "")
    .split(/[.\-_\s]+/)
    .filter(Boolean);
  return `${first?.[0] ?? "O"}${second?.[0] ?? "C"}`.toUpperCase();
}
