import { useEffect, useState } from "react";
import {
  getStorageEstimate,
  requestStoragePersistence,
  type StorageEstimateInfo,
} from "../db/clientMeta";
import type { StoragePersistenceStatus } from "../types/ui";

export function useStoragePersistence(): {
  status: StoragePersistenceStatus;
  estimate: StorageEstimateInfo;
  refreshEstimate: () => Promise<void>;
} {
  const [status, setStatus] = useState<StoragePersistenceStatus>("unknown");
  const [estimate, setEstimate] = useState<StorageEstimateInfo>({
    usageBytes: null,
    quotaBytes: null,
  });

  const refreshEstimate = async () => {
    const next = await getStorageEstimate();
    setEstimate(next);
  };

  useEffect(() => {
    let active = true;
    void (async () => {
      const [persistStatus, est] = await Promise.all([
        requestStoragePersistence(),
        getStorageEstimate(),
      ]);
      if (active) {
        setStatus(persistStatus);
        setEstimate(est);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  return { status, estimate, refreshEstimate };
}
