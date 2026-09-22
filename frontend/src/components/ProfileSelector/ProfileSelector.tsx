import React, { useEffect, useState } from 'react';
import axios from 'axios';

interface ChromeProfile {
  folder_name: string;
  display_name: string;
  email: string;
  avatar_index: number;
  is_using: boolean;
}

interface ProfilesData {
  profiles: ChromeProfile[];
  active_profile: string;
  user_data_dir: string;
}

// Avatar colors for Chrome profile icons
const AVATAR_COLORS = [
  '#10b981', '#3b82f6', '#8b5cf6', '#ec4899',
  '#f59e0b', '#ef4444', '#06b6d4', '#84cc16',
  '#f97316', '#6366f1', '#14b8a6', '#e11d48',
];

export function ProfileSelector() {
  const [data, setData] = useState<ProfilesData | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState(false);

  const fetchProfiles = async () => {
    try {
      const res = await axios.get<ProfilesData>('/api/v1/chrome/profiles');
      setData(res.data);
    } catch (err) {
      console.error('Failed to load Chrome profiles:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchProfiles();
  }, []);

  const handleSelect = async (folderName: string) => {
    if (folderName === data?.active_profile) {
      setOpen(false);
      return;
    }
    setSwitching(true);
    try {
      await axios.post('/api/v1/chrome/profile', { folder_name: folderName });
      await fetchProfiles();
      setOpen(false);
    } catch (err) {
      console.error('Failed to switch profile:', err);
    } finally {
      setSwitching(false);
    }
  };

  if (loading || !data) {
    return (
      <div className="profile-selector-trigger" title="Loading profiles...">
        <div className="profile-avatar-mini" style={{ background: '#333' }}>…</div>
      </div>
    );
  }

  const activeProfile = data.profiles.find((p) => p.is_using) || data.profiles[0];
  const avatarColor = AVATAR_COLORS[activeProfile?.avatar_index % AVATAR_COLORS.length] || AVATAR_COLORS[0];
  const initial = activeProfile?.display_name?.charAt(0)?.toUpperCase() || 'C';

  return (
    <div className="profile-selector-wrap">
      <button
        className="profile-selector-trigger"
        onClick={() => setOpen(!open)}
        title={`Chrome Profile: ${activeProfile?.display_name || 'Default'}`}
      >
        <div className="profile-avatar-mini" style={{ background: avatarColor }}>
          {initial}
        </div>
        <div className="profile-trigger-info">
          <span className="profile-trigger-name">
            {activeProfile?.display_name || 'Default'}
          </span>
          <span className="profile-trigger-email">
            {activeProfile?.email || 'Local Chrome'}
          </span>
        </div>
        <span className="profile-chevron">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="profile-dropdown">
          <div className="profile-dropdown-header">
            <span>🌐 Chrome Profiles</span>
            <span className="profile-count">{data.profiles.length} detected</span>
          </div>
          <div className="profile-dropdown-list">
            {data.profiles.map((profile) => {
              const color = AVATAR_COLORS[profile.avatar_index % AVATAR_COLORS.length];
              const isActive = profile.folder_name === data.active_profile;
              return (
                <button
                  key={profile.folder_name}
                  className={`profile-option ${isActive ? 'active' : ''}`}
                  onClick={() => handleSelect(profile.folder_name)}
                  disabled={switching}
                >
                  <div className="profile-avatar-mini" style={{ background: color }}>
                    {profile.display_name?.charAt(0)?.toUpperCase() || 'C'}
                  </div>
                  <div className="profile-option-info">
                    <span className="profile-option-name">{profile.display_name}</span>
                    <span className="profile-option-email">
                      {profile.email || profile.folder_name}
                    </span>
                  </div>
                  {isActive && <span className="profile-active-badge">Active</span>}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
