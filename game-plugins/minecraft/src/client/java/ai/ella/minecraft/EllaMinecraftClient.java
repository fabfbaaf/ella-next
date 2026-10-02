package ai.ella.minecraft;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.InteractionHand;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.AtomicLong;

public final class EllaMinecraftClient implements ClientModInitializer {
    private static final Gson GSON = new Gson();
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    private final String clientId = "minecraft-" + UUID.randomUUID();
    private volatile String sessionId = "";
    private final AtomicLong observationSeq = new AtomicLong();
    private static final String[] CAPABILITIES = {
        "move", "turn", "look", "jump", "select_hotbar",
        "break_block", "use_item", "attack_entity", "wait"
    };
    private BridgeConfig config;
    private int ticks;
    private float previousHealth = -1f;
    private String previousBiome = "";
    private String previousScreen = "";
    private String previousDimension = "";
    private boolean wasInWorld;
    private long combatUntilMs;
    private record QueuedAction(String sessionId, JsonObject action) {}
    private final AtomicReference<QueuedAction> nextAction = new AtomicReference<>();
    private boolean polling;
    private String activeActionId;
    private String activeActionName;
    private int actionTicks;
    private final ActionWatchdog actionWatchdog = new ActionWatchdog();
    private double startX;
    private double startY;
    private double startZ;
    private float startYaw;
    private boolean observedJump;
    private Entity targetEntity;
    private float targetEntityHealth;
    private BlockPos targetBlock;
    private String targetBlockBefore;
    private int startItemCount;
    private String startScreen;
    private Map<String, Object> lastAction = Map.of();

    @Override
    public void onInitializeClient() {
        this.config = BridgeConfig.load();
        ClientTickEvents.END_CLIENT_TICK.register(this::onTick);
    }

    private void onTick(Minecraft client) {
        this.ticks++;
        if (this.ticks % 100 == 0) this.config = BridgeConfig.load();
        if (this.actionWatchdog.shouldAbort(this.config != null, System.nanoTime())) {
            if (client.player != null && client.level != null) this.completeAction(client, false);
            else this.abortActiveAction(client);
        }
        if (this.config == null) {
            this.nextAction.set(null);
            return;
        }

        boolean inWorld = client.player != null && client.level != null;
        if (inWorld && !this.wasInWorld) {
            this.sessionId = UUID.randomUUID().toString();
            this.observationSeq.set(0L);
            this.nextAction.set(null);
            this.register();
            this.event("minecraft.session.joined", Map.of("text", "Minecraft world session connected to Ella."));
        } else if (!inWorld && this.wasInWorld) {
            this.event("minecraft.session.left", Map.of("text", "Minecraft world session disconnected from Ella."));
            this.abortActiveAction(client);
            this.nextAction.set(null);
            this.sessionId = "";
            this.previousHealth = -1f;
            this.previousBiome = "";
            this.previousScreen = "";
            this.previousDimension = "";
        }
        this.wasInWorld = inWorld;
        if (!inWorld) return;

        this.advanceAction(client);
        QueuedAction command = this.nextAction.getAndSet(null);
        if (command != null && command.sessionId().equals(this.sessionId)
            && this.activeActionId == null) this.beginAction(client, command.action());

        if (this.ticks % 10 == 0 && !this.polling && this.activeActionId == null) this.pollAction();

        if (this.ticks % 20 != 0) return; // 1 Hz telemetry; no per-frame network chatter.
        this.publishState(client);
    }

    private void publishState(Minecraft client) {
        if (client.player == null || client.level == null) return;
        Vec3 pos = client.player.position();
        float health = client.player.getHealth();
        String biome = String.valueOf(client.level.getBiome(client.player.blockPosition()).unwrapKey().orElse(null));
        String screen = client.gui.screen() == null ? "gameplay" : client.gui.screen().getClass().getSimpleName();
        String dimension = client.level.dimension().identifier().toString();
        long now = System.currentTimeMillis();

        if (this.previousHealth >= 0f && health < this.previousHealth) {
            this.combatUntilMs = now + 5_000L;
            this.event("minecraft.player.damaged", Map.of(
                "health", health,
                "delta", health - this.previousHealth
            ));
            if (this.previousHealth > 0f && health <= 0f) {
                this.event("minecraft.player.died", Map.of(
                    "text", "The player died in Minecraft while Isla was present.",
                    "remember", true,
                    "importance", 0.72
                ));
            }
        }
        if (!this.previousBiome.isEmpty() && !this.previousBiome.equals(biome)) {
            this.event("minecraft.world.biome_changed", Map.of("biome", biome));
        }
        if (!this.previousScreen.equals(screen)) {
            this.event("minecraft.screen.changed", Map.of("screen", screen));
        }
        if (!this.previousDimension.isEmpty() && !this.previousDimension.equals(dimension)) {
            this.event("minecraft.world.dimension_changed", Map.of("dimension", dimension));
        }
        this.previousHealth = health;
        this.previousBiome = biome;
        this.previousScreen = screen;
        this.previousDimension = dimension;

        Map<String, Object> player = new LinkedHashMap<>();
        player.put("name", client.player.getName().getString());
        player.put("health", health);
        player.put("max_health", client.player.getMaxHealth());
        player.put("food", client.player.getFoodData().getFoodLevel());
        player.put("experience_level", client.player.experienceLevel);
        player.put("position", Map.of("x", round(pos.x), "y", round(pos.y), "z", round(pos.z)));
        player.put("main_hand", client.player.getMainHandItem().getHoverName().getString());
        player.put("main_hand_count", client.player.getMainHandItem().getCount());
        player.put("selected_slot", client.player.getInventory().getSelectedSlot());
        player.put("yaw", round(client.player.getYRot()));
        player.put("pitch", round(client.player.getXRot()));
        List<Map<String, Object>> inventory = new ArrayList<>();
        for (int slot = 0; slot < Math.min(client.player.getInventory().getContainerSize(), 36); slot++) {
            var item = client.player.getInventory().getItem(slot);
            if (!item.isEmpty()) inventory.add(Map.of(
                "slot", slot, "item", item.getHoverName().getString(), "count", item.getCount()
            ));
        }
        player.put("inventory", inventory);
        Map<String, Object> state = new LinkedHashMap<>();
        state.put("client_id", this.clientId);
        state.put("session_id", this.sessionId);
        state.put("save_id", "session:" + this.sessionId);
        state.put("observation_seq", this.observationSeq.incrementAndGet());
        state.put("full_snapshot", true);
        state.put("integration", "fabric");
        state.put("game_version", gameVersion());
        state.put("mod_version", modVersion());
        state.put("capabilities", CAPABILITIES);
        state.put("player", player);
        if (client.hitResult instanceof BlockHitResult hit) {
            BlockPos target = hit.getBlockPos();
            var blockState = client.level.getBlockState(target);
            if (!blockState.isAir()) state.put("target_block", Map.of(
                "x", target.getX(), "y", target.getY(), "z", target.getZ(),
                "block", String.valueOf(blockState.getBlock())
            ));
        }
        if (client.hitResult instanceof EntityHitResult hit && !hit.getEntity().isRemoved()) {
            Entity entity = hit.getEntity();
            Map<String, Object> target = new LinkedHashMap<>();
            target.put("id", entity.getId());
            target.put("name", entity.getName().getString());
            if (entity instanceof LivingEntity living) target.put("health", living.getHealth());
            state.put("target_entity", target);
        }
        List<Map<String, Object>> nearby = new ArrayList<>();
        BlockPos center = client.player.blockPosition();
        for (int dx = -2; dx <= 2; dx++) for (int dz = -2; dz <= 2; dz++) {
            for (int dy = -1; dy <= 1; dy++) {
                BlockPos block = center.offset(dx, dy, dz);
                var blockState = client.level.getBlockState(block);
                if (!blockState.isAir()) nearby.add(Map.of(
                    "x", block.getX(), "y", block.getY(), "z", block.getZ(),
                    "block", String.valueOf(blockState.getBlock())
                ));
            }
        }
        state.put("nearby_blocks", nearby);
        state.put("biome", biome);
        state.put("dimension", dimension);
        state.put("screen", screen);
        state.put("moving", client.player.getDeltaMovement().lengthSqr() > 0.0025d);
        state.put("exploring", client.player.getDeltaMovement().lengthSqr() > 0.0025d);
        state.put("combat", now < this.combatUntilMs);
        state.put("nearby_players", Math.max(0, client.level.players().size() - 1));
        state.put("multiplayer", client.level.players().size() > 1);
        state.put("world_loaded", true);
        state.put("last_action", this.lastAction);

        this.post("/api/game-plugins/minecraft/state", Map.of("state", state));
    }

    private static String gameVersion() {
        return FabricLoader.getInstance().getModContainer("minecraft")
            .map(mod -> mod.getMetadata().getVersion().getFriendlyString()).orElse("unknown");
    }

    private static String modVersion() {
        return FabricLoader.getInstance().getModContainer("ella-game-bridge")
            .map(mod -> mod.getMetadata().getVersion().getFriendlyString()).orElse("unknown");
    }

    private void register() {
        Map<String, Object> state = new LinkedHashMap<>();
        state.put("client_id", this.clientId);
        state.put("session_id", this.sessionId);
        state.put("save_id", "session:" + this.sessionId);
        state.put("observation_seq", this.observationSeq.incrementAndGet());
        state.put("full_snapshot", false);
        state.put("integration", "fabric");
        state.put("game_version", gameVersion());
        state.put("mod_version", modVersion());
        state.put("capabilities", CAPABILITIES);
        state.put("world_loaded", true);
        this.post("/api/game-plugins/minecraft/state", Map.of("state", state));
    }

    private void event(String type, Map<String, ?> payload) {
        this.post("/api/game-plugins/minecraft/events", Map.of("type", type, "payload", payload));
    }

    private void pollAction() {
        BridgeConfig current = this.config;
        String requestedSession = this.sessionId;
        if (current == null || requestedSession.isEmpty()) return;
        this.polling = true;
        try {
            HttpRequest request = HttpRequest.newBuilder(
                URI.create(current.baseUrl + "/api/game-plugins/minecraft/commands/next?client_id="
                    + URLEncoder.encode(this.clientId, StandardCharsets.UTF_8)
                    + "&session_id=" + URLEncoder.encode(requestedSession, StandardCharsets.UTF_8)))
                .timeout(Duration.ofSeconds(2))
                .header("Authorization", "Bearer " + current.token)
                .GET().build();
            this.http.sendAsync(request, HttpResponse.BodyHandlers.ofString())
                .thenAccept(response -> {
                    if (response.statusCode() != 200) return;
                    try {
                        JsonObject root = GSON.fromJson(response.body(), JsonObject.class);
                        if (root != null && root.has("client_id") && root.has("session_id")
                            && this.clientId.equals(root.get("client_id").getAsString())
                            && requestedSession.equals(root.get("session_id").getAsString())
                            && requestedSession.equals(this.sessionId)
                            && root.has("action") && root.get("action").isJsonObject())
                            this.nextAction.set(new QueuedAction(requestedSession, root.getAsJsonObject("action")));
                    } catch (RuntimeException ignored) { }
                }).whenComplete((result, error) -> this.polling = false);
        } catch (RuntimeException error) {
            this.polling = false;
        }
    }

    private void beginAction(Minecraft client, JsonObject action) {
        if (client.player == null) return;
        try {
            String id = action.get("action_id").getAsString();
            String name = action.get("name").getAsString();
            JsonObject parameters = action.getAsJsonObject("parameters");
            if (id.isBlank() || parameters == null) return;
            this.activeActionId = id;
            this.activeActionName = name;
            this.actionWatchdog.start(System.nanoTime());
            Vec3 pos = client.player.position();
            this.startX = pos.x;
            this.startY = pos.y;
            this.startZ = pos.z;
            this.startYaw = client.player.getYRot();
            this.observedJump = false;
            this.targetBlock = client.hitResult instanceof BlockHitResult hit ? hit.getBlockPos() : null;
            this.targetBlockBefore = this.targetBlock == null ? null
                : String.valueOf(client.level.getBlockState(this.targetBlock));
            this.targetEntity = client.hitResult instanceof EntityHitResult hit ? hit.getEntity() : null;
            this.targetEntityHealth = this.targetEntity instanceof LivingEntity living
                ? living.getHealth() : -1f;
            this.startItemCount = client.player.getMainHandItem().getCount();
            this.startScreen = client.gui.screen() == null ? "" : client.gui.screen().getClass().getName();
            if ("turn".equals(name)) {
                client.player.setYRot(this.startYaw + parameters.get("degrees").getAsFloat());
                this.completeAction(client, Math.abs(client.player.getYRot() - this.startYaw) > 0.1f);
                return;
            }
            if ("look".equals(name)) {
                float yaw = this.startYaw + parameters.get("yaw_delta").getAsFloat();
                float pitch = Math.max(-89f, Math.min(89f,
                    client.player.getXRot() + parameters.get("pitch_delta").getAsFloat()));
                float previousPitch = client.player.getXRot();
                client.player.setYRot(yaw);
                client.player.setXRot(pitch);
                this.completeAction(client,
                    Math.abs(client.player.getYRot() - this.startYaw) > 0.1f
                    || Math.abs(client.player.getXRot() - previousPitch) > 0.1f);
                return;
            }
            if ("select_hotbar".equals(name)) {
                int slot = parameters.get("slot").getAsInt();
                if (slot < 0 || slot > 8) {
                    this.completeAction(client, false);
                    return;
                }
                client.player.getInventory().setSelectedSlot(slot);
                this.completeAction(client, client.player.getInventory().getSelectedSlot() == slot);
                return;
            }
            if ("break_block".equals(name) && this.targetBlock == null) {
                this.completeAction(client, false);
                return;
            }
            int count = "use_item".equals(name) ? 5
                : "attack_entity".equals(name) ? 8 : parameters.get("ticks").getAsInt();
            this.actionTicks = Math.max(1, Math.min(count, 100));
            if ("move".equals(name)) {
                String direction = parameters.get("direction").getAsString();
                switch (direction) {
                    case "forward" -> client.options.keyUp.setDown(true);
                    case "back" -> client.options.keyDown.setDown(true);
                    case "left" -> client.options.keyLeft.setDown(true);
                    case "right" -> client.options.keyRight.setDown(true);
                    default -> this.completeAction(client, false);
                }
            } else if ("jump".equals(name)) {
                client.options.keyJump.setDown(true);
            } else if ("break_block".equals(name)) {
                client.options.keyAttack.setDown(true);
            } else if ("use_item".equals(name)) {
                client.options.keyUse.setDown(true);
            } else if ("attack_entity".equals(name)) {
                if (this.targetEntity == null || client.gameMode == null) {
                    this.completeAction(client, false);
                    return;
                }
                client.gameMode.attack(client.player, this.targetEntity);
                client.player.swing(InteractionHand.MAIN_HAND);
            } else if ("wait".equals(name)) {
                // The elapsed game ticks are the observable result of this action.
            } else {
                this.completeAction(client, false);
            }
        } catch (RuntimeException error) {
            if (this.activeActionId != null) this.completeAction(client, false);
        }
    }

    private void advanceAction(Minecraft client) {
        if (this.activeActionId == null || client.player == null) return;
        Vec3 pos = client.player.position();
        if ("jump".equals(this.activeActionName) && Math.abs(pos.y - this.startY) > 0.05)
            this.observedJump = true;
        if (--this.actionTicks > 0) return;
        boolean moved = Math.hypot(pos.x - this.startX, pos.z - this.startZ) > 0.08;
        boolean blockChanged = this.targetBlock != null && !String.valueOf(
            client.level.getBlockState(this.targetBlock)).equals(this.targetBlockBefore);
        boolean used = client.player.getMainHandItem().getCount() != this.startItemCount
            || blockChanged || (client.gui.screen() != null
                && !client.gui.screen().getClass().getName().equals(this.startScreen));
        boolean attacked = this.targetEntity != null && (
            this.targetEntity.isRemoved()
            || (this.targetEntity instanceof LivingEntity living
                && this.targetEntityHealth >= 0f
                && living.getHealth() < this.targetEntityHealth)
        );
        boolean verified = switch (this.activeActionName) {
            case "jump" -> this.observedJump;
            case "break_block" -> blockChanged;
            case "use_item" -> used;
            case "attack_entity" -> attacked;
            case "wait" -> true;
            default -> moved;
        };
        this.completeAction(client, verified);
    }

    private void releaseControls(Minecraft client) {
        client.options.keyUp.setDown(false);
        client.options.keyDown.setDown(false);
        client.options.keyLeft.setDown(false);
        client.options.keyRight.setDown(false);
        client.options.keyJump.setDown(false);
        client.options.keyAttack.setDown(false);
        client.options.keyUse.setDown(false);
    }

    private void abortActiveAction(Minecraft client) {
        this.releaseControls(client);
        this.actionWatchdog.stop();
        if (this.activeActionId != null) this.lastAction = Map.of(
            "action_id", this.activeActionId, "status", "unverified"
        );
        this.activeActionId = null;
        this.activeActionName = null;
        this.actionTicks = 0;
        this.targetEntity = null;
    }

    private void completeAction(Minecraft client, boolean verified) {
        String id = this.activeActionId;
        if (id == null) return;
        this.releaseControls(client);
        this.actionWatchdog.stop();
        this.lastAction = Map.of("action_id", id, "status", verified ? "verified" : "unverified");
        this.activeActionId = null;
        this.activeActionName = null;
        this.actionTicks = 0;
        this.targetEntity = null;
        Vec3 pos = client.player.position();
        Map<String, Object> player = new LinkedHashMap<>();
        player.put("position", Map.of("x", pos.x, "y", pos.y, "z", pos.z));
        player.put("health", client.player.getHealth());
        player.put("selected_slot", client.player.getInventory().getSelectedSlot());
        player.put("main_hand", client.player.getMainHandItem().getHoverName().getString());
        player.put("main_hand_count", client.player.getMainHandItem().getCount());
        player.put("yaw", round(client.player.getYRot()));
        player.put("pitch", round(client.player.getXRot()));
        Map<String, Object> state = new LinkedHashMap<>();
        state.put("client_id", this.clientId);
        state.put("session_id", this.sessionId);
        state.put("save_id", "session:" + this.sessionId);
        state.put("observation_seq", this.observationSeq.incrementAndGet());
        state.put("full_snapshot", false);
        state.put("player", player);
        state.put("world_loaded", true);
        state.put("last_action", this.lastAction);
        if (client.hitResult instanceof BlockHitResult hit) {
            BlockPos target = hit.getBlockPos();
            var blockState = client.level.getBlockState(target);
            state.put("target_block", blockState.isAir() ? null : Map.of(
                "x", target.getX(), "y", target.getY(), "z", target.getZ(),
                "block", String.valueOf(blockState.getBlock())
            ));
        } else {
            state.put("target_block", null);
        }
        if (client.hitResult instanceof EntityHitResult hit && !hit.getEntity().isRemoved()) {
            Entity entity = hit.getEntity();
            Map<String, Object> target = new LinkedHashMap<>();
            target.put("id", entity.getId());
            target.put("name", entity.getName().getString());
            if (entity instanceof LivingEntity living) target.put("health", living.getHealth());
            state.put("target_entity", target);
        } else {
            state.put("target_entity", null);
        }
        this.post("/api/game-plugins/minecraft/commands/receipt", Map.of(
            "action_id", id, "verified", verified, "state", state
        ));
    }

    private void post(String path, Object body) {
        BridgeConfig current = this.config;
        if (current == null) return;
        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create(current.baseUrl + path))
                .timeout(Duration.ofSeconds(2))
                .header("Authorization", "Bearer " + current.token)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(GSON.toJson(body), StandardCharsets.UTF_8))
                .build();
            CompletableFuture<HttpResponse<String>> future = this.http.sendAsync(request, HttpResponse.BodyHandlers.ofString());
            future.exceptionally(error -> null);
        } catch (RuntimeException ignored) {
            // Runtime can be restarted independently; the next telemetry tick retries naturally.
        }
    }

    private static double round(double value) {
        return Math.round(value * 100.0d) / 100.0d;
    }

    private static final class BridgeConfig {
        final String baseUrl;
        final String token;

        BridgeConfig(String baseUrl, String token) {
            this.baseUrl = baseUrl;
            this.token = token;
        }

        static BridgeConfig load() {
            String local = System.getenv("LOCALAPPDATA");
            Path path = local == null
                ? Path.of(System.getProperty("user.home"), ".local", "share", "ella-next", "game-bridge", "minecraft.json")
                : Path.of(local, "EllaNext", "game-bridge", "minecraft.json");
            String configuredData = System.getenv("ELLA_DATA_DIR");
            if (configuredData != null && !configuredData.isBlank())
                path = Path.of(configuredData).toAbsolutePath().resolve("game-bridge").resolve("minecraft.json");
            if (!Files.isRegularFile(path)) return null;
            try {
                JsonObject root = GSON.fromJson(Files.readString(path), JsonObject.class);
                String token = root.get("token").getAsString();
                if (token.length() < 20) return null;
                String baseUrl = root.has("base_url") ? root.get("base_url").getAsString() : "http://127.0.0.1:8766";
                URI uri = URI.create(baseUrl);
                if (!"http".equals(uri.getScheme()) || !"127.0.0.1".equals(uri.getHost())
                    || uri.getPort() < 1 || uri.getPort() > 65535 || uri.getRawUserInfo() != null
                    || !(uri.getPath().isEmpty() || "/".equals(uri.getPath()))
                    || uri.getRawQuery() != null || uri.getRawFragment() != null) return null;
                return new BridgeConfig(baseUrl.replaceAll("/$", ""), token);
            } catch (IOException | RuntimeException ignored) {
                return null;
            }
        }
    }
}
