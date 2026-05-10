import { aiUgcFetch } from "./ai-ugc-api";

export const AUTOMATION_BACKEND_URL =
  process.env.NEXT_PUBLIC_AUTOMATION_BACKEND_URL || "";

export const POSTBRIDGE_DASHBOARD_URL =
  process.env.NEXT_PUBLIC_POSTBRIDGE_DASHBOARD_URL || "https://app.post-bridge.com";

export type ProductResearchPlan = {
  product?: {
    name?: string;
    summary?: string;
    positioning?: string;
    targetCustomers?: string[];
    buyingTriggers?: string[];
    objections?: string[];
  };
  marketInsights?: string[];
  contentAngles?: Array<{
    title?: string;
    insight?: string;
    whyItWorks?: string;
    hookIdeas?: string[];
    talkingPoints?: string[];
    proofPoints?: string[];
    cta?: string;
  }>;
  hookBank?: string[];
  ctas?: string[];
  researchNotes?: string[];
};

export type ResearchProject = {
  id: string;
  product_name?: string | null;
  product_url?: string | null;
  audience?: string | null;
  product_context?: string | null;
  data?: ProductResearchPlan;
  plan?: ProductResearchPlan;
  created_at?: string | null;
  updated_at?: string | null;
};

export type SavedProduct = {
  id: string;
  name: string;
  description?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type ImageCollection = {
  id: string;
  name: string;
  image_count?: number;
  is_virtual?: boolean;
  is_community?: boolean;
  recent_images?: Array<{
    id: string;
    image_url: string;
    name?: string;
  }>;
};

export type SlideshowDraft = {
  id: string;
  title?: string | null;
  description?: string | null;
  slideshow_data?: {
    slides?: Array<{
      id: string;
      imageUrl: string;
      text_elements?: Array<{
        text: string;
        position?: { x: number; y: number };
        font_size?: number;
        font_color?: string;
        width?: number;
        height?: number;
        text_align?: string;
      }>;
    }>;
  };
};

export type GeneratedSlideshow = {
  id: string;
  title?: string | null;
  description?: string | null;
  status: "processing" | "completed" | "failed";
  job_id?: string | null;
  original_slideshow_id?: string | null;
  total_slides?: number;
  completed_slides?: number;
  generated_images?: Array<{
    image_url?: string;
    url?: string;
    slide_number?: number;
  }>;
  error_message?: string | null;
  created_at?: string;
};

export type AutomationBatchItem = {
  position: number;
  hook: string;
  draft?: SlideshowDraft;
  generated_slideshow?: GeneratedSlideshow;
  generated_slideshow_id?: string;
  generatedSlideshowId?: string;
  job_id?: string;
  jobId?: string;
  status?: "processing" | "completed" | "failed";
  generated_images?: GeneratedSlideshow["generated_images"];
  error_message?: string | null;
};

export type AutomationBatchStatus = {
  id: string;
  status: "processing" | "completed" | "failed" | "partial";
  total: number;
  completed: number;
  failed: number;
};

export type TikTokAccount = {
  id: string | number;
  platform?: string;
  username?: string;
  name?: string;
  display_name?: string;
  avatar_url?: string;
};

export type TikTokPostResult = {
  generated_slideshow_id: string;
  position: number;
  postbridge_post_id?: string;
  media_ids?: string[];
  scheduled_at?: string | null;
  skipped?: boolean;
};

export type PostBridgePost = {
  id: string;
  caption?: string | null;
  status?: string | null;
  scheduled_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  social_accounts?: Array<string | number>;
  media?: string[];
  platform_configurations?: Record<string, unknown>;
};

export type PostBridgePublishResult = {
  id: string;
  post_id?: string;
  success?: boolean;
  social_account_id?: string | number;
  error?: unknown;
  platform_data?: {
    id?: string;
    url?: string;
    username?: string;
  };
};

export type AutomationResult = {
  batchId?: string;
  slideshowCount?: number;
  researchProject: ResearchProject;
  selectedHook: string;
  rankedHooks: string[];
  selectedCollection: ImageCollection;
  draft: SlideshowDraft;
  generatedSlideshowId: string;
  jobId: string;
  slideshows?: AutomationBatchItem[];
};

export type AutomationInput =
  | {
      source: "new";
      productName: string;
      productUrl?: string;
      audience?: string;
      productContext?: string;
    }
  | {
      source: "research";
      researchProjectId: string;
    }
  | {
      source: "product";
      product: SavedProduct;
    };

export type AutomationStage = "research" | "hook" | "collection" | "draft" | "export";

type BackendAutomationResponse = {
  success: boolean;
  batchId?: string;
  slideshowCount?: number;
  researchProject: ResearchProject;
  selectedHook: string;
  rankedHooks: string[];
  selectedCollection: ImageCollection;
  draft: SlideshowDraft;
  generatedSlideshowId: string;
  jobId: string;
  slideshows?: AutomationBatchItem[];
};

function getPlan(project: ResearchProject): ProductResearchPlan {
  return project.data || project.plan || {};
}

function normalizeHooks(items: Array<string | undefined | null>): string[] {
  const seen = new Set<string>();
  return items
    .map((item) => String(item || "").trim())
    .filter((item) => {
      if (!item) return false;
      const key = item.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function selectRankedResearchHooks(project: ResearchProject): string[] {
  const plan = getPlan(project);
  const hookBank = Array.isArray(plan.hookBank) ? plan.hookBank : [];
  const angleHooks = Array.isArray(plan.contentAngles)
    ? plan.contentAngles.flatMap((angle) => Array.isArray(angle.hookIdeas) ? angle.hookIdeas : [])
    : [];

  return normalizeHooks([...hookBank, ...angleHooks]);
}

export function autoPickCollection(collections: ImageCollection[], communityCollections: ImageCollection[]): ImageCollection | null {
  const valid = [...communityCollections, ...collections].filter((collection) => {
    const imageCount = typeof collection.image_count === "number"
      ? collection.image_count
      : collection.recent_images?.length || 0;

    return (
      imageCount > 0 &&
      !collection.is_virtual &&
      collection.name !== "Uncollected" &&
      collection.name !== "Hooks"
    );
  });

  return valid[0] || null;
}

async function loadFreshResearch(input: Extract<AutomationInput, { source: "new" | "product" }>): Promise<ResearchProject> {
  if (input.source === "product") {
    const response = await aiUgcFetch<ResearchProject>("/product-research", {
      method: "POST",
      body: JSON.stringify({
        productName: input.product.name,
        productUrl: "",
        audience: "",
        productContext: input.product.description || "",
      }),
    });
    return response;
  }

  const response = await aiUgcFetch<ResearchProject>("/product-research", {
    method: "POST",
    body: JSON.stringify({
      productName: input.productName,
      productUrl: input.productUrl || "",
      audience: input.audience || "",
      productContext: input.productContext || "",
    }),
  });

  return response;
}

export async function listResearchProjects(): Promise<ResearchProject[]> {
  const response = await aiUgcFetch<{ data: ResearchProject[] }>("/product-research/projects");
  return response.data || [];
}

export async function listProducts(): Promise<SavedProduct[]> {
  const response = await aiUgcFetch<{ success: boolean; products: SavedProduct[] }>("/products");
  return response.products || [];
}

export async function getGeneratedSlideshows(): Promise<GeneratedSlideshow[]> {
  const response = await aiUgcFetch<{ success: boolean; slideshows: GeneratedSlideshow[] }>("/slideshows/generate-images?limit=30");
  return response.slideshows || [];
}

async function automationBackendFetch<T>(
  path: string,
  accessToken: string,
  init: RequestInit = {},
): Promise<T> {
  if (!AUTOMATION_BACKEND_URL) {
    throw new Error("NEXT_PUBLIC_AUTOMATION_BACKEND_URL is not configured.");
  }

  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const response = await fetch(`${AUTOMATION_BACKEND_URL.replace(/\/$/, "")}${normalizedPath}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
      ...(init.headers || {}),
    },
  });

  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail || payload?.error || `Automation backend request failed with ${response.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  return payload as T;
}

export async function listAutomationBackendContext(accessToken: string): Promise<{
  researchProjects: ResearchProject[];
  products: SavedProduct[];
}> {
  return automationBackendFetch("/automation/products/context", accessToken);
}

export async function getAutomationBackendGeneratedSlideshow(
  generatedSlideshowId: string,
  accessToken: string,
): Promise<GeneratedSlideshow | null> {
  const response = await automationBackendFetch<{ generated_slideshow: GeneratedSlideshow }>(
    `/automation/product-slideshow/${generatedSlideshowId}`,
    accessToken,
  );
  return response.generated_slideshow || null;
}

export async function getAutomationBackendBatchStatus(
  batchId: string,
  accessToken: string,
): Promise<{ batch: AutomationBatchStatus; items: AutomationBatchItem[] }> {
  return automationBackendFetch<{ batch: AutomationBatchStatus; items: AutomationBatchItem[] }>(
    `/automation/product-slideshow-batches/${batchId}`,
    accessToken,
  );
}

export async function listAutomationBackendTikTokAccounts(accessToken: string): Promise<TikTokAccount[]> {
  const response = await automationBackendFetch<{ data: TikTokAccount[] }>(
    "/automation/postbridge/tiktok-accounts",
    accessToken,
  );
  return response.data || [];
}

export async function postAutomationBackendBatchToTikTok(
  batchId: string,
  accessToken: string,
  input: {
    socialAccountIds: Array<string | number>;
    caption?: string;
    scheduledStartAt?: string;
    scheduleFrequency?: "daily" | "interval";
    intervalMinutes?: number;
  },
): Promise<{ success: boolean; batchId: string; posts: TikTokPostResult[] }> {
  return automationBackendFetch(`/automation/product-slideshow-batches/${batchId}/post-to-tiktok`, accessToken, {
    method: "POST",
    body: JSON.stringify({
      social_account_ids: input.socialAccountIds,
      caption: input.caption || undefined,
      scheduled_start_at: input.scheduledStartAt || undefined,
      schedule_frequency: input.scheduleFrequency || "daily",
      interval_minutes: input.intervalMinutes ?? 1440,
    }),
  });
}

export async function listAutomationBackendPostBridgePosts(accessToken: string): Promise<PostBridgePost[]> {
  const response = await automationBackendFetch<{ data: PostBridgePost[] }>(
    "/automation/postbridge/posts",
    accessToken,
  );
  return response.data || [];
}

export async function listAutomationBackendPostBridgeResults(accessToken: string): Promise<PostBridgePublishResult[]> {
  const response = await automationBackendFetch<{ data: PostBridgePublishResult[]; warning?: string | null }>(
    "/automation/postbridge/post-results",
    accessToken,
  );
  if (response.warning) {
    console.warn(response.warning);
  }
  return response.data || [];
}

export async function listAutomationBackendPostBridgeResultsWithWarning(accessToken: string): Promise<{
  data: PostBridgePublishResult[];
  warning?: string | null;
}> {
  return automationBackendFetch<{ data: PostBridgePublishResult[]; warning?: string | null }>(
    "/automation/postbridge/post-results",
    accessToken,
  );
}

export async function runBackendProductSlideshowAutomation(
  input: AutomationInput,
  accessToken: string,
  onStage?: (stage: AutomationStage) => void,
): Promise<AutomationResult> {
  onStage?.("research");

  const body = input.source === "new"
    ? {
        source: "new",
        product_name: input.productName,
        product_url: input.productUrl || "",
        audience: input.audience || "",
        product_context: input.productContext || "",
      }
    : input.source === "research"
      ? {
          source: "research",
          research_project_id: input.researchProjectId,
        }
      : {
          source: "product",
          product_id: input.product.id,
        };

  onStage?.("hook");
  onStage?.("collection");
  onStage?.("draft");
  onStage?.("export");

  const response = await automationBackendFetch<BackendAutomationResponse>(
    "/automation/product-slideshow",
    accessToken,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );

  return {
    batchId: response.batchId,
    slideshowCount: response.slideshowCount,
    researchProject: response.researchProject,
    selectedHook: response.selectedHook,
    rankedHooks: response.rankedHooks,
    selectedCollection: response.selectedCollection,
    draft: response.draft,
    generatedSlideshowId: response.generatedSlideshowId,
    jobId: response.jobId,
    slideshows: response.slideshows,
  };
}

export async function runProductSlideshowAutomation(
  input: AutomationInput,
  onStage?: (stage: AutomationStage) => void,
): Promise<AutomationResult> {
  onStage?.("research");
  const researchProject = input.source === "research"
    ? await aiUgcFetch<ResearchProject>(`/product-research/projects/${input.researchProjectId}`)
    : await loadFreshResearch(input);

  onStage?.("hook");
  const rankedHooks = selectRankedResearchHooks(researchProject);
  const selectedHook = rankedHooks[0];
  if (!selectedHook) {
    throw new Error("Product research did not return any usable hooks.");
  }

  onStage?.("collection");
  const collectionResponse = await aiUgcFetch<{
    success: boolean;
    collections: ImageCollection[];
    communityCollections: ImageCollection[];
  }>("/settings/slideshow-collections");
  const selectedCollection = autoPickCollection(
    collectionResponse.collections || [],
    collectionResponse.communityCollections || [],
  );

  if (!selectedCollection) {
    throw new Error("No non-empty slideshow image collection is available.");
  }

  onStage?.("draft");
  const plan = getPlan(researchProject);
  const draft = await aiUgcFetch<SlideshowDraft>("/hook-product-slideshows/generate", {
    method: "POST",
    body: JSON.stringify({
      selectedHook,
      productName: researchProject.product_name || plan.product?.name || "",
      productDescription: researchProject.product_context || plan.product?.summary || "",
      productResearchProjectId: researchProject.id,
      imageCollectionId: selectedCollection.id,
    }),
  });

  onStage?.("export");
  const exportResponse = await aiUgcFetch<{
    success: boolean;
    jobId: string;
    generatedSlideshowId: string;
  }>("/slideshows/generate-images", {
    method: "POST",
    body: JSON.stringify({
      slideshowId: draft.id,
      title: `${researchProject.product_name || plan.product?.name || "Product"} - Automated Slideshow`,
      description: `Automated from product research using hook: ${selectedHook}`,
    }),
  });

  return {
    researchProject,
    selectedHook,
    rankedHooks,
    selectedCollection,
    draft,
    generatedSlideshowId: exportResponse.generatedSlideshowId,
    jobId: exportResponse.jobId,
  };
}
