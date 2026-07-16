import { useQuery, keepPreviousData } from '@tanstack/react-query'
import {
  getActivityStats,
  getActivity,
  getReviewItems,
  getFolderRules,
  getEntities,
  searchEntities,
  getDocuments,
  getApiUsage,
  getGoogleStatus,
  getDriveRootStatus,
  getGmailWatchStatus,
} from '../api/client'

export function useActivityStats() {
  return useQuery({
    queryKey: ['activity', 'stats'],
    queryFn: getActivityStats,
    staleTime: 20_000,
    refetchInterval: (query) =>
      query.state.data?.processing_count > 0 ? 10_000 : 30_000,
  })
}

export function useActivity(offset = 0) {
  return useQuery({
    queryKey: ['activity', offset],
    queryFn: () => getActivity({ limit: 50, offset }),
    staleTime: 25_000,
    // Keep the list live while the tab is open: poll every 30s, but only the first
    // page (offset 0) — paged-in older rows don't change, so don't re-poll them.
    refetchInterval: offset === 0 ? 30_000 : false,
    refetchIntervalInBackground: false,
  })
}

export function useReviewItems(status = 'pending', offset = 0) {
  return useQuery({
    queryKey: ['review', 'items', status, offset],
    queryFn: () => getReviewItems(status, { limit: 50, offset }),
    staleTime: 15_000,
    // New review items should surface on their own — poll the first page every 30s.
    refetchInterval: offset === 0 ? 30_000 : false,
    refetchIntervalInBackground: false,
  })
}

export function useFolderRules() {
  return useQuery({
    queryKey: ['folder-rules'],
    queryFn: getFolderRules,
    staleTime: 300_000,
  })
}

export function useEntities(query = '') {
  return useQuery({
    queryKey: ['entities', query],
    queryFn: () => (query ? searchEntities(query, 50) : getEntities()),
    staleTime: 120_000,
  })
}

export function useDocuments({ q = '', entity = '', limit = 50, offset = 0 } = {}) {
  return useQuery({
    queryKey: ['documents', { q, entity, limit, offset }],
    queryFn: () => getDocuments({ q: q.trim(), entity, limit, offset }),
    staleTime: 30_000,
    // Keep the previous page visible while a new filter/search query loads,
    // so the table doesn't flash empty on every keystroke.
    placeholderData: keepPreviousData,
  })
}

export function useApiUsage() {
  return useQuery({
    queryKey: ['api-usage'],
    queryFn: getApiUsage,
    staleTime: 60_000,
  })
}

// Integration status (Settings → Integrations). Live Google/Drive checks are slow,
// so cache them and share the cache across the panel's three cards.
export function useGoogleStatus() {
  return useQuery({ queryKey: ['google', 'status'], queryFn: getGoogleStatus, staleTime: 60_000 })
}

export function useDriveRootStatus() {
  return useQuery({ queryKey: ['google', 'drive-root'], queryFn: getDriveRootStatus, staleTime: 60_000 })
}

export function useGmailWatchStatus() {
  return useQuery({ queryKey: ['google', 'watch'], queryFn: getGmailWatchStatus, staleTime: 60_000 })
}
