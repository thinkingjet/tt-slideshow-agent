"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  AutomationResult,
  AutomationInput,
  AUTOMATION_BACKEND_URL,
  AutomationBatchItem,
  AutomationBatchStatus,
  GeneratedSlideshow,
  POSTBRIDGE_DASHBOARD_URL,
  PostBridgePost,
  PostBridgePublishResult,
  ResearchProject,
  SavedProduct,
  TikTokAccount,
  getAutomationBackendBatchStatus,
  getAutomationBackendGeneratedSlideshow,
  getGeneratedSlideshows,
  listAutomationBackendPostBridgePosts,
  listAutomationBackendPostBridgeResultsWithWarning,
  listAutomationBackendTikTokAccounts,
  listAutomationBackendContext,
  listProducts,
  listResearchProjects,
  postAutomationBackendBatchToTikTok,
  runBackendProductSlideshowAutomation,
  runProductSlideshowAutomation,
  selectRankedResearchHooks,
} from "@/lib/automation";
import { aiUgcAppUrl, aiUgcDownloadUrl } from "@/lib/ai-ugc-api";

type SourceMode = "new" | "research" | "product";
type PageMode = "automation" | "calendar";
type StepStatus = "idle" | "active" | "done" | "error";

type Step = {
  id: string;
  label: string;
  detail: string;
  status: StepStatus;
};

const initialSteps: Step[] = [
  { id: "research", label: "Research", detail: "Analyze product and customer context", status: "idle" },
  { id: "hook", label: "Hook", detail: "Choose the strongest research hook", status: "idle" },
  { id: "collection", label: "Backgrounds", detail: "Auto-pick a non-empty image collection", status: "idle" },
  { id: "draft", label: "Draft", detail: "Generate the hook + product slideshow", status: "idle" },
  { id: "export", label: "Export", detail: "Start rendered slide export", status: "idle" },
  { id: "ready", label: "Ready", detail: "Show the final slideshow", status: "idle" },
];

function setActiveStep(steps: Step[], id: string): Step[] {
  const activeIndex = steps.findIndex((step) => step.id === id);
  return steps.map((step, index) => ({
    ...step,
    status: index < activeIndex ? "done" : step.id === id ? "active" : "idle",
  }));
}

function setStepError(steps: Step[]): Step[] {
  return steps.map((step) => step.status === "active" ? { ...step, status: "error" } : step);
}

function generatedImageUrl(image: NonNullable<GeneratedSlideshow["generated_images"]>[number]): string {
  return image.image_url || image.url || "";
}

function tiktokAccountLabel(account: TikTokAccount): string {
  return account.display_name || account.username || account.name || `TikTok account ${account.id}`;
}

function localDateTimeToIso(value: string): string | undefined {
  if (!value.trim()) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function postDate(post: PostBridgePost): string {
  return post.scheduled_at || post.created_at || post.updated_at || "";
}

function formatPostDate(value: string): string {
  if (!value) return "Unscheduled";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unscheduled";
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function postResultForPost(results: PostBridgePublishResult[], postId: string): PostBridgePublishResult | null {
  return results.find((result) => result.post_id === postId) || null;
}

function monthLabel(date: Date): string {
  return new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(date);
}

function dayKey(date: Date): string {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

function monthCells(month: Date): Date[] {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const start = new Date(first);
  start.setDate(first.getDate() - first.getDay());
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    return date;
  });
}

function compactCaption(caption?: string | null): string {
  const cleaned = String(caption || "Untitled TikTok post").replace(/\s+/g, " ").trim();
  return cleaned.length > 72 ? `${cleaned.slice(0, 69)}...` : cleaned;
}

export default function AutomationPage() {
  const [pageMode, setPageMode] = useState<PageMode>("automation");
  const [sourceMode, setSourceMode] = useState<SourceMode>("new");
  const [productName, setProductName] = useState("");
  const [productUrl, setProductUrl] = useState("");
  const [audience, setAudience] = useState("");
  const [productContext, setProductContext] = useState("");
  const [selectedResearchId, setSelectedResearchId] = useState("");
  const [selectedProductId, setSelectedProductId] = useState("");
  const [tiktokAccounts, setTikTokAccounts] = useState<TikTokAccount[]>([]);
  const [selectedTikTokAccountId, setSelectedTikTokAccountId] = useState("");
  const [tiktokCaption, setTikTokCaption] = useState("");
  const [scheduleStartAt, setScheduleStartAt] = useState("");
  const [postingStatus, setPostingStatus] = useState<"idle" | "posting" | "posted" | "error">("idle");
  const [postingMessage, setPostingMessage] = useState("");
  const [calendarPosts, setCalendarPosts] = useState<PostBridgePost[]>([]);
  const [postResults, setPostResults] = useState<PostBridgePublishResult[]>([]);
  const [isCalendarLoading, setIsCalendarLoading] = useState(false);
  const [calendarError, setCalendarError] = useState("");
  const [calendarWarning, setCalendarWarning] = useState("");
  const [calendarMonth, setCalendarMonth] = useState(() => new Date());
  const [researchProjects, setResearchProjects] = useState<ResearchProject[]>([]);
  const [products, setProducts] = useState<SavedProduct[]>([]);
  const [isLoadingLists, setIsLoadingLists] = useState(true);
  const [steps, setSteps] = useState(initialSteps);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<AutomationResult | null>(null);
  const [generatedSlideshow, setGeneratedSlideshow] = useState<GeneratedSlideshow | null>(null);
  const [batchStatus, setBatchStatus] = useState<AutomationBatchStatus | null>(null);
  const [batchItems, setBatchItems] = useState<AutomationBatchItem[]>([]);

  const useAutomationBackend = Boolean(AUTOMATION_BACKEND_URL);

  const selectedResearchProject = useMemo(
    () => researchProjects.find((project) => project.id === selectedResearchId) || null,
    [researchProjects, selectedResearchId],
  );

  const selectedProduct = useMemo(
    () => products.find((product) => product.id === selectedProductId) || null,
    [products, selectedProductId],
  );

  const previewHooks = selectedResearchProject
    ? selectRankedResearchHooks(selectedResearchProject).slice(0, 4)
    : [];

  useEffect(() => {
    let isMounted = true;

    async function loadLists() {
      setIsLoadingLists(true);
      try {
        const context = useAutomationBackend
          ? await listAutomationBackendContext()
          : null;
        const [loadedResearchProjects, loadedProducts] = context
          ? [context.researchProjects, context.products]
          : await Promise.all([
              listResearchProjects(),
              listProducts(),
            ]);
        const loadedTikTokAccounts = useAutomationBackend
          ? await listAutomationBackendTikTokAccounts().catch((accountError) => {
              setPostingMessage(accountError instanceof Error ? accountError.message : "Could not load TikTok accounts.");
              return [];
            })
          : [];

        if (!isMounted) return;
        setResearchProjects(loadedResearchProjects);
        setProducts(loadedProducts);
        setTikTokAccounts(loadedTikTokAccounts);
        setSelectedResearchId((current) => current || loadedResearchProjects[0]?.id || "");
        setSelectedProductId((current) => current || loadedProducts[0]?.id || "");
        setSelectedTikTokAccountId((current) => current || String(loadedTikTokAccounts[0]?.id || ""));
      } catch (loadError) {
        if (!isMounted) return;
        setError(loadError instanceof Error ? loadError.message : "Could not load products.");
      } finally {
        if (isMounted) setIsLoadingLists(false);
      }
    }

    loadLists();

    return () => {
      isMounted = false;
    };
  }, [useAutomationBackend]);

  useEffect(() => {
    if (!useAutomationBackend || pageMode !== "calendar") return;
    let isMounted = true;

    async function loadCalendar() {
      setIsCalendarLoading(true);
      setCalendarError("");
      setCalendarWarning("");
      try {
        const posts = await listAutomationBackendPostBridgePosts();
        const results = await listAutomationBackendPostBridgeResultsWithWarning();
        if (!isMounted) return;
        setCalendarPosts(posts);
        setPostResults(results.data || []);
        setCalendarWarning(results.warning || "");
      } catch (loadError) {
        if (!isMounted) return;
        setCalendarError(loadError instanceof Error ? loadError.message : "Could not load scheduled posts.");
      } finally {
        if (isMounted) setIsCalendarLoading(false);
      }
    }

    loadCalendar();

    return () => {
      isMounted = false;
    };
  }, [pageMode, useAutomationBackend]);

  async function refreshTikTokAccounts() {
    if (!useAutomationBackend) return;
    setPostingMessage("");
    try {
      const accounts = await listAutomationBackendTikTokAccounts();
      setTikTokAccounts(accounts);
      setSelectedTikTokAccountId((current) => current || String(accounts[0]?.id || ""));
      setPostingMessage(accounts.length ? `${accounts.length} TikTok account${accounts.length === 1 ? "" : "s"} connected.` : "No connected TikTok accounts found yet.");
    } catch (accountError) {
      setPostingMessage(accountError instanceof Error ? accountError.message : "Could not refresh TikTok accounts.");
    }
  }

  useEffect(() => {
    if (!result?.batchId && !result?.generatedSlideshowId) return;

    const batchId = result.batchId;
    const generatedSlideshowId = result.generatedSlideshowId;
    let isMounted = true;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    async function pollExport() {
      try {
        if (useAutomationBackend && batchId) {
          const currentBatch = await getAutomationBackendBatchStatus(batchId);
          if (!isMounted) return;
          setBatchStatus(currentBatch.batch);
          setBatchItems(currentBatch.items);
          if (currentBatch.batch.status === "completed") {
            setSteps((currentSteps) => currentSteps.map((step) => ({ ...step, status: "done" })));
            if (selectedTikTokAccountId && postingStatus === "idle") {
              setPostingStatus("posting");
              setPostingMessage("Posting completed exports to TikTok via PostBridge...");
              try {
                const postResult = await postAutomationBackendBatchToTikTok(batchId, {
                  socialAccountIds: [selectedTikTokAccountId],
                  caption: tiktokCaption.trim(),
                  scheduledStartAt: localDateTimeToIso(scheduleStartAt),
                  scheduleFrequency: "daily",
                  intervalMinutes: 1440,
                });
                if (!isMounted) return;
                setPostingStatus("posted");
                setPostingMessage(`${postResult.posts.length} TikTok posts created in PostBridge.`);
              } catch (postError) {
                if (!isMounted) return;
                setPostingStatus("error");
                setPostingMessage(postError instanceof Error ? postError.message : "TikTok posting failed.");
              }
            }
            return;
          }
          if (currentBatch.batch.status === "failed") {
            setSteps(setStepError);
            setError("All slideshow exports failed.");
            return;
          }
          timeoutId = setTimeout(pollExport, 3000);
          return;
        }

        if (!generatedSlideshowId) {
          timeoutId = setTimeout(pollExport, 3000);
          return;
        }

        const current = useAutomationBackend
          ? await getAutomationBackendGeneratedSlideshow(generatedSlideshowId)
          : (await getGeneratedSlideshows()).find((slideshow) => slideshow.id === generatedSlideshowId);

        if (!isMounted) return;
        if (current) {
          setGeneratedSlideshow(current);
          if (current.status === "completed") {
            setSteps((currentSteps) => currentSteps.map((step) => ({ ...step, status: "done" })));
            return;
          }
          if (current.status === "failed") {
            setSteps(setStepError);
            setError(current.error_message || "Slideshow export failed.");
            return;
          }
        }

        timeoutId = setTimeout(pollExport, 3000);
      } catch {
        timeoutId = setTimeout(pollExport, 5000);
      }
    }

    setSteps((currentSteps) => setActiveStep(currentSteps, "ready"));
    pollExport();

    return () => {
      isMounted = false;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, [postingStatus, result?.batchId, result?.generatedSlideshowId, scheduleStartAt, selectedTikTokAccountId, tiktokCaption, useAutomationBackend]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setResult(null);
    setGeneratedSlideshow(null);
    setBatchStatus(null);
    setBatchItems([]);
    setPostingStatus("idle");
    setPostingMessage("");
    setIsRunning(true);
    setSteps(setActiveStep(initialSteps, "research"));

    try {
      if (sourceMode === "new" && !productName.trim()) {
        throw new Error("Enter a product name to start the automation.");
      }
      if (sourceMode === "research" && !selectedResearchId) {
        throw new Error("Select a researched product to start the automation.");
      }
      if (sourceMode === "product" && !selectedProduct) {
        throw new Error("Select a saved product to start the automation.");
      }
      if (useAutomationBackend && !selectedTikTokAccountId) {
        throw new Error("Connect/select a TikTok account before running the posting automation.");
      }

      const automationInput: AutomationInput = sourceMode === "new"
          ? {
              source: "new",
              productName: productName.trim(),
              productUrl: productUrl.trim(),
              audience: audience.trim(),
              productContext: productContext.trim(),
            }
          : sourceMode === "research"
            ? { source: "research", researchProjectId: selectedResearchId }
            : { source: "product", product: selectedProduct as SavedProduct };
      const automationResult = useAutomationBackend
        ? await runBackendProductSlideshowAutomation(
            automationInput,
            (stage) => setSteps((currentSteps) => setActiveStep(currentSteps, stage)),
          )
        : await runProductSlideshowAutomation(
            automationInput,
            (stage) => setSteps((currentSteps) => setActiveStep(currentSteps, stage)),
          );

      setResult(automationResult);
      if (automationResult.slideshows?.length) {
        setBatchItems(automationResult.slideshows);
      }
      setSteps((currentSteps) => setActiveStep(currentSteps, "ready"));
    } catch (runError) {
      setSteps(setStepError);
      setError(runError instanceof Error ? runError.message : "Automation failed.");
    } finally {
      setIsRunning(false);
    }
  }

  const exportedImages = generatedSlideshow?.generated_images?.map(generatedImageUrl).filter(Boolean) || [];
  const firstDraftSlide = result?.draft.slideshow_data?.slides?.[0];
  const returnedBatchItems = batchItems.length ? batchItems : result?.slideshows || [];
  const batchSize = result?.slideshowCount || returnedBatchItems.length;
  const visibleBatchItems = batchSize > 1
    ? Array.from({ length: batchSize }, (_, index) => {
        return returnedBatchItems.find((item) => item.position === index + 1) || {
          position: index + 1,
          hook: result?.rankedHooks[index] || (index === 0 ? result?.selectedHook : "") || "Generating hook...",
          status: "processing" as const,
        };
      })
    : returnedBatchItems;
  const isBatchRun = visibleBatchItems.length > 1;
  const sortedCalendarPosts = [...calendarPosts].sort((a, b) => {
    const left = new Date(postDate(a)).getTime() || 0;
    const right = new Date(postDate(b)).getTime() || 0;
    return left - right;
  });
  const calendarPostsByDay = sortedCalendarPosts.reduce<Record<string, PostBridgePost[]>>((groups, post) => {
    const value = postDate(post);
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return groups;
    const key = dayKey(date);
    groups[key] = [...(groups[key] || []), post];
    return groups;
  }, {});
  const calendarDays = monthCells(calendarMonth);
  const todayKey = dayKey(new Date());

  return (
    <main className="shell">
      <section className="hero">
        <div className="eyebrow">One-click automation</div>
        <h1>Turn product research into an exported TikTok slideshow.</h1>
        <p>
          Enter a product or pick an existing one. The automation researches it, chooses the top hook,
          generates a hook + product slideshow, exports it, and shows the final slide deck.
        </p>
      </section>

      <nav className="mode-switch">
        <button type="button" className={pageMode === "automation" ? "active" : ""} onClick={() => setPageMode("automation")}>
          Automation
        </button>
        <button type="button" className={pageMode === "calendar" ? "active" : ""} onClick={() => setPageMode("calendar")}>
          Scheduled calendar
        </button>
      </nav>

      {pageMode === "calendar" ? (
        <section className="panel calendar-panel">
          <div className="result-header">
            <div>
              <div className="eyebrow">PostBridge calendar</div>
              <h2>Calendar</h2>
              <p>These are posts currently visible through PostBridge for the connected account.</p>
            </div>
            <div className="actions">
              <button type="button" onClick={() => setCalendarMonth((current) => new Date(current.getFullYear(), current.getMonth() - 1, 1))}>Prev</button>
              <button type="button" onClick={() => setCalendarMonth(new Date())}>{monthLabel(calendarMonth)}</button>
              <button type="button" onClick={() => setCalendarMonth((current) => new Date(current.getFullYear(), current.getMonth() + 1, 1))}>Next</button>
              <button type="button" onClick={() => setPageMode("automation")}>New automation</button>
              <button type="button" onClick={async () => {
                if (!useAutomationBackend) return;
                setIsCalendarLoading(true);
                setCalendarError("");
                setCalendarWarning("");
                try {
                  const posts = await listAutomationBackendPostBridgePosts();
                  const results = await listAutomationBackendPostBridgeResultsWithWarning();
                  setCalendarPosts(posts);
                  setPostResults(results.data || []);
                  setCalendarWarning(results.warning || "");
                } catch (refreshError) {
                  setCalendarError(refreshError instanceof Error ? refreshError.message : "Could not refresh calendar.");
                } finally {
                  setIsCalendarLoading(false);
                }
              }}>
                Refresh
              </button>
            </div>
          </div>

          {!useAutomationBackend ? (
            <div className="empty-viewer">Configure the automation backend env vars first so PostBridge posts can load.</div>
          ) : calendarError ? (
            <div className="error">{calendarError}</div>
          ) : isCalendarLoading ? (
            <div className="empty-viewer">Loading scheduled posts...</div>
          ) : sortedCalendarPosts.length ? (
            <>
              {calendarWarning ? <div className="warning">{calendarWarning}</div> : null}
              <div className="month-calendar">
                {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((day) => (
                  <div className="weekday" key={day}>{day}</div>
                ))}
                {calendarDays.map((date) => {
                  const key = dayKey(date);
                  const posts = calendarPostsByDay[key] || [];
                  const isMuted = date.getMonth() !== calendarMonth.getMonth();
                  const isToday = key === todayKey;
                  return (
                    <div className={`calendar-day ${isMuted ? "muted" : ""} ${isToday ? "today" : ""}`} key={key}>
                      <div className="day-number">{date.getDate()}</div>
                      <div className="day-events">
                        {posts.length ? posts.map((post) => {
                          const result = postResultForPost(postResults, post.id);
                          const status = post.status || (result?.success ? "published" : "scheduled");
                          return (
                            <a
                              className={`calendar-event ${result?.success ? "published" : ""}`}
                              href={result?.platform_data?.url || "#"}
                              target={result?.platform_data?.url ? "_blank" : undefined}
                              rel={result?.platform_data?.url ? "noreferrer" : undefined}
                              key={post.id}
                              onClick={(event) => {
                                if (!result?.platform_data?.url) event.preventDefault();
                              }}
                              title={post.caption || "Untitled TikTok post"}
                            >
                              <span>{formatPostDate(postDate(post)).split(",").pop()?.trim() || status}</span>
                              <strong>{compactCaption(post.caption)}</strong>
                            </a>
                          );
                        }) : <span className="no-posts">No posts</span>}
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <>
              {calendarWarning ? <div className="warning">{calendarWarning}</div> : null}
              <div className="empty-viewer">No scheduled TikTok posts found yet.</div>
            </>
          )}
        </section>
      ) : (
      <>
      <section className="grid">
        <form className="panel form-panel" onSubmit={handleSubmit}>
          <div className="panel-heading">
            <h2>Product Source</h2>
            <span>{useAutomationBackend ? "Railway backend" : isLoadingLists ? "Loading saved products..." : "Local proxy"}</span>
          </div>

          {AUTOMATION_BACKEND_URL ? (
            <div className="backend-card">
              <strong>Railway automation backend configured</strong>
              <p>{AUTOMATION_BACKEND_URL}</p>
              {useAutomationBackend ? (
                <div className="posting-card">
                  <strong>TikTok posting</strong>
                  {tiktokAccounts.length ? (
                    <>
                      <label>
                        Connected TikTok account
                        <select value={selectedTikTokAccountId} onChange={(event) => setSelectedTikTokAccountId(event.target.value)}>
                          {tiktokAccounts.map((account) => (
                            <option key={account.id} value={String(account.id)}>
                              {tiktokAccountLabel(account)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Caption override
                        <textarea
                          value={tiktokCaption}
                          onChange={(event) => setTikTokCaption(event.target.value)}
                          placeholder="Leave blank to use each slideshow hook + link in bio."
                        />
                      </label>
                      <label>
                        Daily posting time
                        <input type="datetime-local" value={scheduleStartAt} onChange={(event) => setScheduleStartAt(event.target.value)} />
                      </label>
                      <p className="schedule-note">
                        The 7 exports will be scheduled once per day at this same time, starting on the selected date.
                      </p>
                      <div className="mini-actions">
                        <button type="button" onClick={refreshTikTokAccounts}>Refresh accounts</button>
                        <a href={POSTBRIDGE_DASHBOARD_URL} target="_blank" rel="noreferrer">Manage PostBridge</a>
                      </div>
                    </>
                  ) : (
                    <>
                      <p>Connect a TikTok account in PostBridge, then refresh this page.</p>
                      <div className="mini-actions">
                        <a href={POSTBRIDGE_DASHBOARD_URL} target="_blank" rel="noreferrer">Connect TikTok in PostBridge</a>
                        <button type="button" onClick={refreshTikTokAccounts}>Refresh accounts</button>
                      </div>
                    </>
                  )}
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="segmented">
            <button type="button" className={sourceMode === "new" ? "active" : ""} onClick={() => setSourceMode("new")}>
              New product
            </button>
            <button type="button" className={sourceMode === "research" ? "active" : ""} onClick={() => setSourceMode("research")}>
              Researched product
            </button>
            <button type="button" className={sourceMode === "product" ? "active" : ""} onClick={() => setSourceMode("product")}>
              Saved product
            </button>
          </div>

          {sourceMode === "new" ? (
            <div className="fields">
              <label>
                Product name
                <input value={productName} onChange={(event) => setProductName(event.target.value)} placeholder="e.g. GlowMist humidifier" />
              </label>
              <label>
                Product URL
                <input value={productUrl} onChange={(event) => setProductUrl(event.target.value)} placeholder="https://..." />
              </label>
              <label>
                Audience
                <input value={audience} onChange={(event) => setAudience(event.target.value)} placeholder="e.g. apartment renters, beauty buyers" />
              </label>
              <label>
                Context
                <textarea value={productContext} onChange={(event) => setProductContext(event.target.value)} placeholder="Claims, differentiators, offer, product notes..." />
              </label>
            </div>
          ) : sourceMode === "research" ? (
            <div className="fields">
              <label>
                Existing research
                <select value={selectedResearchId} onChange={(event) => setSelectedResearchId(event.target.value)}>
                  {researchProjects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.product_name || project.data?.product?.name || "Untitled research"}
                    </option>
                  ))}
                </select>
              </label>
              {previewHooks.length > 0 ? (
                <div className="preview-card">
                  <strong>Likely hook:</strong>
                  <p>{previewHooks[0]}</p>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="fields">
              <label>
                Saved product
                <select value={selectedProductId} onChange={(event) => setSelectedProductId(event.target.value)}>
                  {products.map((product) => (
                    <option key={product.id} value={product.id}>
                      {product.name}
                    </option>
                  ))}
                </select>
              </label>
              {selectedProduct?.description ? (
                <div className="preview-card">
                  <strong>Product context:</strong>
                  <p>{selectedProduct.description}</p>
                </div>
              ) : null}
            </div>
          )}

          {error ? <div className="error">{error}</div> : null}
          {postingMessage ? <div className={postingStatus === "error" ? "error" : "preview-card"}>{postingMessage}</div> : null}

          <button className="primary" type="submit" disabled={isRunning || isLoadingLists}>
            {isRunning ? "Running automation..." : "Research, Generate, and Export"}
          </button>
        </form>

        <aside className="panel">
          <div className="panel-heading">
            <h2>Automation Status</h2>
            <span>
              {batchStatus
                ? `${batchStatus.completed}/${batchStatus.total} complete`
                : generatedSlideshow?.status || (result ? "exporting" : "idle")}
            </span>
          </div>
          <div className="timeline">
            {steps.map((step) => (
              <div key={step.id} className={`step ${step.status}`}>
                <div className="dot" />
                <div>
                  <strong>{step.label}</strong>
                  <p>{step.detail}</p>
                </div>
              </div>
            ))}
          </div>
          <div className="storage-card">
            <strong>Saved automatically</strong>
            <p>Research is stored in <code>product_research_projects</code>, drafts in <code>slideshows</code>, exports in <code>user_generated_slideshows</code>, and PostBridge post/media IDs in <code>processing_metadata.postbridge</code>.</p>
          </div>
        </aside>
      </section>

      {result ? (
        <section className="panel result-panel">
          <div className="result-header">
            <div>
              <div className="eyebrow">Selected hook</div>
              <h2>{result.selectedHook}</h2>
              <p>
                Product: {result.researchProject.product_name || result.researchProject.data?.product?.name || "Unknown"}.
                Background collection: {result.selectedCollection.name}.
                {result.slideshowCount ? ` Creating ${result.slideshowCount} slideshow exports.` : ""}
              </p>
            </div>
            <div className="actions">
              <a href={aiUgcAppUrl(`/hook-product-slideshows`)} target="_blank" rel="noreferrer">
                Open editor
              </a>
              {generatedSlideshow?.id && generatedSlideshow.status === "completed" ? (
                <a href={aiUgcDownloadUrl(`/slideshows/download-zip/${generatedSlideshow.id}`)}>
                  Download ZIP
                </a>
              ) : null}
            </div>
          </div>

          {isBatchRun ? (
            <div className="batch-grid">
              {visibleBatchItems.map((item, itemIndex) => {
                const generated = item.generated_slideshow;
                const images = (generated?.generated_images || item.generated_images || [])
                  .map(generatedImageUrl)
                  .filter(Boolean);
                const previewImage = images[0] || item.draft?.slideshow_data?.slides?.[0]?.imageUrl;
                const status = generated?.status || item.status || "processing";
                return (
                  <article className="batch-card" key={item.generated_slideshow_id || item.generatedSlideshowId || item.job_id || item.jobId || itemIndex}>
                    <div className="batch-preview">
                      {previewImage ? <img src={previewImage} alt={`Slideshow ${item.position || itemIndex + 1} preview`} /> : <div className="empty-viewer">Processing...</div>}
                    </div>
                    <div className="batch-card-body">
                      <div className="batch-card-topline">
                        <span>#{item.position || itemIndex + 1}</span>
                        <strong>{status}</strong>
                      </div>
                      <h3>{item.hook || "Generating hook..."}</h3>
                      <p>{images.length ? `${images.length} exported slides ready` : "Export is still rendering"}</p>
                      {generated?.id && status === "completed" ? (
                        <a href={aiUgcDownloadUrl(`/slideshows/download-zip/${generated.id}`)}>
                          Download ZIP
                        </a>
                      ) : null}
                    </div>
                  </article>
                );
              })}
            </div>
          ) : null}

          {!isBatchRun ? (
          <div className="result-grid">
            <div className="hook-list">
              <h3>Ranked hooks</h3>
              {result.rankedHooks.slice(0, 5).map((hook, index) => (
                <div className="hook-row" key={hook}>
                  <span>{index + 1}</span>
                  <p>{hook}</p>
                </div>
              ))}
            </div>

            <div className="viewer">
              {exportedImages.length > 0 ? (
                <div className="slides">
                  {exportedImages.map((imageUrl, index) => (
                    <img key={`${imageUrl}-${index}`} src={imageUrl} alt={`Generated slideshow slide ${index + 1}`} />
                  ))}
                </div>
              ) : firstDraftSlide ? (
                <div className="draft-preview" style={{ backgroundImage: `url(${firstDraftSlide.imageUrl})` }}>
                  {firstDraftSlide.text_elements?.map((textElement, index) => (
                    <span key={`${textElement.text}-${index}`}>{textElement.text}</span>
                  ))}
                </div>
              ) : (
                <div className="empty-viewer">Waiting for export output...</div>
              )}
            </div>
          </div>
          ) : null}
        </section>
      ) : null}
      </>
      )}
    </main>
  );
}
