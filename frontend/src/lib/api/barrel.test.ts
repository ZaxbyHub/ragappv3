import { describe, expect, it } from "vitest";
import * as api from "../api";
import apiClient from "../api";

describe("api barrel", () => {
  it("provides a default export (apiClient)", () => {
    expect(apiClient).toBeDefined();
    expect(typeof apiClient.get).toBe("function");
    expect(typeof apiClient.post).toBe("function");
  });

  it("re-exports core exports", () => {
    expect(typeof api.getHealth).toBe("function");
    expect(typeof api.getSettings).toBe("function");
    expect(typeof api.listVaults).toBe("function");
    expect(typeof api.listOrganizations).toBe("function");
  });

  it("re-exports tags exports", () => {
    expect(typeof api.listTags).toBe("function");
    expect(typeof api.createTag).toBe("function");
    expect(typeof api.updateTag).toBe("function");
    expect(typeof api.deleteTag).toBe("function");
    expect(typeof api.assignTags).toBe("function");
    expect(typeof api.getDocumentTags).toBe("function");
    expect(typeof api.setDocumentTags).toBe("function");
    expect(typeof api.unassignTag).toBe("function");
  });

  it("re-exports folders exports", () => {
    expect(typeof api.listFolders).toBe("function");
    expect(typeof api.createFolder).toBe("function");
    expect(typeof api.updateFolder).toBe("function");
    expect(typeof api.deleteFolder).toBe("function");
    expect(typeof api.moveDocumentsToFolder).toBe("function");
  });

  it("re-exports sessions exports", () => {
    expect(typeof api.parseSSEStream).toBe("function");
    expect(typeof api.chatStream).toBe("function");
    expect(typeof api.listChatSessions).toBe("function");
    expect(typeof api.getChatSession).toBe("function");
    expect(typeof api.createChatSession).toBe("function");
    expect(typeof api.addChatMessage).toBe("function");
    expect(typeof api.updateMessageFeedback).toBe("function");
    expect(typeof api.updateChatSession).toBe("function");
    expect(typeof api.deleteChatSession).toBe("function");
    expect(typeof api.forkChatSession).toBe("function");
    expect(typeof api.getChatHistory).toBe("function");
    expect(typeof api.saveChatHistory).toBe("function");
  });

  it("re-exports groups exports", () => {
    expect(typeof api.listGroups).toBe("function");
    expect(typeof api.createGroup).toBe("function");
    expect(typeof api.updateGroup).toBe("function");
    expect(typeof api.deleteGroup).toBe("function");
    expect(typeof api.getGroupMembers).toBe("function");
    expect(typeof api.updateGroupMembers).toBe("function");
    expect(typeof api.getEligibleGroupMembers).toBe("function");
    expect(typeof api.getGroupVaults).toBe("function");
    expect(typeof api.updateGroupVaults).toBe("function");
  });

  it("re-exports users exports", () => {
    expect(typeof api.listAllUsers).toBe("function");
    expect(typeof api.getUserGroups).toBe("function");
    expect(typeof api.updateUserGroups).toBe("function");
  });

  it("re-exports vault-groups exports", () => {
    expect(typeof api.getVaultGroups).toBe("function");
    expect(typeof api.updateVaultGroups).toBe("function");
  });

  it("re-exports wiki exports", () => {
    expect(typeof api.listWikiPages).toBe("function");
    expect(typeof api.getWikiPage).toBe("function");
    expect(typeof api.createWikiPage).toBe("function");
    expect(typeof api.updateWikiPage).toBe("function");
    expect(typeof api.deleteWikiPage).toBe("function");
    expect(typeof api.listWikiEntities).toBe("function");
    expect(typeof api.listWikiClaims).toBe("function");
    expect(typeof api.listWikiLintFindings).toBe("function");
    expect(typeof api.runWikiLint).toBe("function");
    expect(typeof api.searchWiki).toBe("function");
    expect(typeof api.promoteMemoryToWiki).toBe("function");
    expect(typeof api.listWikiJobs).toBe("function");
    expect(typeof api.getWikiJob).toBe("function");
    expect(typeof api.retryWikiJob).toBe("function");
    expect(typeof api.cancelWikiJob).toBe("function");
    expect(typeof api.recompileVaultWiki).toBe("function");
    expect(typeof api.getDocumentWikiStatus).toBe("function");
    expect(typeof api.compileDocumentWiki).toBe("function");
    expect(typeof api.getMemoryWikiStatus).toBe("function");
    expect(typeof api.getWikiPageVersions).toBe("function");
    expect(typeof api.getWikiPageFiles).toBe("function");
    expect(typeof api.attachWikiPageFile).toBe("function");
    expect(typeof api.detachWikiPageFile).toBe("function");
    expect(typeof api.getWikiPageBacklinks).toBe("function");
    expect(typeof api.getWikiActivityFeed).toBe("function");
    expect(typeof api.bulkWikiPageAction).toBe("function");
    expect(typeof api.resolveWikiLintFinding).toBe("function");
  });

  it("re-exports kms exports", () => {
    expect(typeof api.listKMSEntries).toBe("function");
    expect(typeof api.getKMSEntry).toBe("function");
    expect(typeof api.createKMSEntry).toBe("function");
    expect(typeof api.updateKMSEntry).toBe("function");
    expect(typeof api.deleteKMSEntry).toBe("function");
    expect(typeof api.searchKMS).toBe("function");
    expect(typeof api.compileDocumentKMS).toBe("function");
    expect(typeof api.recompileVaultKMS).toBe("function");
    expect(typeof api.listKMSJobs).toBe("function");
    expect(typeof api.downloadDocument).toBe("function");
  });

  it("re-exports health exports", () => {
    expect(typeof api.getHealth).toBe("function");
    expect(typeof api.getLlmModeHealth).toBe("function");
    expect(typeof api.testConnections).toBe("function");
  });

  it("re-exports settings exports", () => {
    expect(typeof api.getSettings).toBe("function");
    expect(typeof api.testCuratorConnection).toBe("function");
    expect(typeof api.updateSettings).toBe("function");
  });

  it("re-exports vaults exports", () => {
    expect(typeof api.listVaults).toBe("function");
    expect(typeof api.listAccessibleVaults).toBe("function");
    expect(typeof api.getVault).toBe("function");
    expect(typeof api.createVault).toBe("function");
    expect(typeof api.updateVault).toBe("function");
    expect(typeof api.deleteVault).toBe("function");
    expect(typeof api.toggleVaultEnrichment).toBe("function");
  });

  it("re-exports memories exports", () => {
    expect(typeof api.searchMemories).toBe("function");
    expect(typeof api.addMemory).toBe("function");
    expect(typeof api.deleteMemory).toBe("function");
    expect(typeof api.updateMemory).toBe("function");
    expect(typeof api.listMemories).toBe("function");
  });

  it("re-exports auth-sessions exports", () => {
    expect(typeof api.changePassword).toBe("function");
    expect(typeof api.listSessions).toBe("function");
    expect(typeof api.revokeSession).toBe("function");
    expect(typeof api.revokeAllSessions).toBe("function");
  });
});
