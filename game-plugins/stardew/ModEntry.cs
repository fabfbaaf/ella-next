using System.Net.Http.Headers;
using System.Collections.Concurrent;
using System.Reflection;
using System.Text;
using System.Text.Json;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Menus;
using xTile.Dimensions;

namespace Ella.StardewBridge;

internal sealed class ModEntry : Mod
{
    private readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(2) };
    private readonly OrderedPostQueue postQueue = new();
    private readonly string clientId = $"stardew-{Guid.NewGuid():N}";
    private BridgeConfig? bridge;
    private float previousHealth = -1f;
    private bool previousStory;
    private bool previousFestival;
    private long combatUntilMs;
    private readonly ConcurrentQueue<(string SessionId, JsonElement Action)> commands = new();
    private string sessionId = "";
    private long observationSeq;
    private int polling;
    private int ticks;
    private string? activeActionId;
    private string? activeActionName;
    private int actionTicks;
    private float startX;
    private float startY;
    private float startEnergy;
    private string? startSpeaker;
    private bool interactionAccepted;
    private string? startMenu;
    private string? startLocation;
    private int startItemCount;
    private DialogueBox? questionMenu;
    private string? questionText;
    private ItemGrabMenu? transferMenu;
    private string? transferItemId;
    private int transferSourceCount;
    private int transferDestinationCount;
    private string? purchaseItemId;
    private int purchaseItemCount;
    private int purchaseMoney;
    private int purchasePrice;
    private object lastAction = new { action_id = "", status = "none" };

    public override void Entry(IModHelper helper)
    {
        this.bridge = BridgeConfig.TryLoad();
        helper.Events.GameLoop.SaveLoaded += this.OnSaveLoaded;
        helper.Events.GameLoop.DayStarted += this.OnDayStarted;
        helper.Events.GameLoop.ReturnedToTitle += this.OnReturnedToTitle;
        helper.Events.GameLoop.UpdateTicked += this.OnUpdateTicked;
        helper.Events.Player.Warped += this.OnWarped;
        helper.Events.Player.LevelChanged += this.OnLevelChanged;
        helper.Events.Display.MenuChanged += this.OnMenuChanged;
    }

    private void OnSaveLoaded(object? sender, SaveLoadedEventArgs e)
    {
        this.bridge = BridgeConfig.TryLoad();
        this.commands.Clear();
        this.sessionId = Guid.NewGuid().ToString("N");
        Interlocked.Exchange(ref this.observationSeq, 0);
        this.Register();
        this.Emit("stardew.session.loaded", new { save = Constants.SaveFolderName });
        this.PublishState();
    }

    private void OnDayStarted(object? sender, DayStartedEventArgs e)
    {
        this.Emit("stardew.day.started", new
        {
            season = Game1.currentSeason,
            day = Game1.dayOfMonth,
            year = Game1.year
        });
        this.PublishState();
    }

    private void OnReturnedToTitle(object? sender, ReturnedToTitleEventArgs e)
    {
        this.Emit("stardew.session.returned_to_title", new { });
        Game1.player?.Halt();
        this.activeActionId = null;
        this.activeActionName = null;
        this.actionTicks = 0;
        this.commands.Clear();
        this.sessionId = "";
        this.previousHealth = -1f;
        this.previousStory = false;
        this.previousFestival = false;
    }

    private void OnWarped(object? sender, WarpedEventArgs e)
    {
        if (!e.IsLocalPlayer)
            return;
        string destination = e.NewLocation?.NameOrUniqueName ?? "unknown";
        this.Emit("stardew.location.warped", new
        {
            from = e.OldLocation?.NameOrUniqueName,
            to = destination,
            mine_floor = ParseMineFloor(destination)
        });
        this.PublishState();
    }

    private void OnLevelChanged(object? sender, LevelChangedEventArgs e)
    {
        if (!e.IsLocalPlayer)
            return;
        this.Emit("stardew.skill.level_up", new
        {
            skill = e.Skill.ToString(),
            old_level = e.OldLevel,
            new_level = e.NewLevel,
            remember = true,
            importance = 0.66
        });
    }

    private void OnMenuChanged(object? sender, MenuChangedEventArgs e)
    {
        string? menu = e.NewMenu?.GetType().Name;
        if (!string.IsNullOrWhiteSpace(menu) && menu.Contains("Dialogue", StringComparison.OrdinalIgnoreCase))
        {
            this.Emit("stardew.dialogue.started", new
            {
                menu,
                npc = Game1.currentSpeaker?.Name
            });
        }
        this.PublishState();
    }

    private void OnUpdateTicked(object? sender, UpdateTickedEventArgs e)
    {
        this.ticks++;
        if (Context.IsWorldReady)
        {
            this.AdvanceAction();
            if (this.activeActionId is null && this.commands.TryDequeue(out var queued)
                && queued.SessionId == this.sessionId)
                this.BeginAction(queued.Action);
            if (this.ticks % 30 == 0 && this.activeActionId is null)
                this.PollAction();
        }
        if (!e.IsOneSecond)
            return;
        // Runtime restarts rotate both the port and token. Re-read even when a
        // previous endpoint exists; never keep retrying an obsolete endpoint.
        BridgeConfig? latest = BridgeConfig.TryLoad();
        if (latest != this.bridge)
        {
            this.bridge = latest;
            if (latest is not null && Context.IsWorldReady)
                this.Register();
        }
        if (!Context.IsWorldReady)
            return;
        this.PublishState();
    }

    private void PublishState()
    {
        if (!Context.IsWorldReady || this.bridge is null)
            return;

        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        float health = Game1.player.health;
        if (this.previousHealth >= 0 && health < this.previousHealth)
        {
            this.combatUntilMs = now + 5000;
            this.Emit("stardew.player.damaged", new { health, delta = health - this.previousHealth });
        }
        this.previousHealth = health;

        bool story = Game1.eventUp;
        bool festival = this.TryGetCurrentEventFlag("isFestival");
        if (story && !this.previousStory)
        {
            this.Emit("stardew.story.started", new
            {
                location = Game1.currentLocation?.NameOrUniqueName,
                remember = true,
                importance = 0.70
            });
        }
        if (festival && !this.previousFestival)
        {
            this.Emit("stardew.festival.started", new
            {
                location = Game1.currentLocation?.NameOrUniqueName,
                remember = true,
                importance = 0.72
            });
        }
        this.previousStory = story;
        this.previousFestival = festival;

        string location = Game1.currentLocation?.NameOrUniqueName ?? "unknown";
        string menu = Game1.activeClickableMenu?.GetType().Name ?? "gameplay";
        bool dialogue = menu.Contains("Dialogue", StringComparison.OrdinalIgnoreCase);
        bool inMines = location.Contains("Mine", StringComparison.OrdinalIgnoreCase)
            || location.Contains("Skull", StringComparison.OrdinalIgnoreCase);
        bool onFarm = location.Contains("Farm", StringComparison.OrdinalIgnoreCase);
        int tileX = Game1.player.TilePoint.X;
        int tileY = Game1.player.TilePoint.Y;
        var nearbyObjects = Game1.currentLocation?.Objects.Pairs
            .Where(entry => Math.Abs(entry.Key.X - tileX) <= 4
                && Math.Abs(entry.Key.Y - tileY) <= 4)
            .Take(32)
            .Select(entry => new
            {
                x = (int)entry.Key.X, y = (int)entry.Key.Y,
                item = entry.Value.DisplayName
            }).ToArray();
        var nearbyTerrain = Game1.currentLocation?.terrainFeatures.Pairs
            .Where(entry => Math.Abs(entry.Key.X - tileX) <= 4
                && Math.Abs(entry.Key.Y - tileY) <= 4)
            .Take(32)
            .Select(entry => new
            {
                x = (int)entry.Key.X, y = (int)entry.Key.Y,
                feature = entry.Value.GetType().Name
            }).ToArray();

        var state = new
        {
            client_id = this.clientId,
            session_id = this.sessionId,
            save_id = Constants.SaveFolderName,
            observation_seq = Interlocked.Increment(ref this.observationSeq),
            full_snapshot = true,
            integration = "smapi",
            game_version = Game1.version,
            mod_version = this.ModManifest.Version.ToString(),
            capabilities = Capabilities,
            player = new
            {
                name = Game1.player.Name,
                health = Game1.player.health,
                max_health = Game1.player.maxHealth,
                energy = Math.Round(Game1.player.Stamina, 1),
                max_energy = Game1.player.MaxStamina,
                money = Game1.player.Money,
                tile = new { x = Game1.player.TilePoint.X, y = Game1.player.TilePoint.Y },
                facing = Game1.player.FacingDirection,
                current_tool = Game1.player.CurrentTool?.DisplayName,
                selected_slot = Game1.player.CurrentToolIndex
            },
            inventory = Game1.player.Items.Select((item, slot) => item is null ? null
                : new { slot, item_id = item.QualifiedItemId,
                    name = item.DisplayName, count = item.Stack })
                .Where(item => item is not null).Take(36).ToArray(),
            nearby_objects = nearbyObjects,
            nearby_terrain = nearbyTerrain,
            location,
            date = new { season = Game1.currentSeason, day = Game1.dayOfMonth, year = Game1.year },
            time = Game1.timeOfDay,
            weather = Game1.weatherIcon,
            menu,
            dialogue,
            dialogue_text = (Game1.activeClickableMenu as DialogueBox)?.getCurrentString(),
            dialogue_question = (Game1.activeClickableMenu as DialogueBox)?.isQuestion ?? false,
            dialogue_can_continue = this.CanContinueDialogue(),
            dialogue_can_choose = this.CanChooseDialogue(),
            dialogue_options = this.DialogueOptions(),
            chest_can_transfer = this.CanTransferChest(),
            chest_items = this.ChestItems(),
            shop_items = this.ShopItems(),
            shop_currency = (Game1.activeClickableMenu as ShopMenu)?.currency,
            menu_closable = this.CanCloseMenu(),
            player_free = Context.IsPlayerFree && Context.CanPlayerMove,
            cutscene = story,
            story_event = story,
            festival,
            farming = onFarm && Context.IsPlayerFree,
            farm_planning = onFarm && !dialogue,
            exploring = Context.CanPlayerMove && !onFarm,
            mine_combat = inMines && now < this.combatUntilMs,
            in_combat = now < this.combatUntilMs,
            multiplayer = Context.IsMultiplayer,
            nearby_players = Game1.getOnlineFarmers().Count - 1,
            world_ready = true,
            last_action = this.lastAction
        };

        this.Post("/api/game-plugins/stardew_valley/state", new { state });
    }

    private static int? ParseMineFloor(string location)
    {
        const string prefix = "UndergroundMine";
        if (!location.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            return null;
        return int.TryParse(location[prefix.Length..], out int floor) ? floor : null;
    }

    private bool TryGetCurrentEventFlag(string member)
    {
        try
        {
            object? location = Game1.currentLocation;
            if (location is null)
                return false;
            object? currentEvent = location.GetType().GetField("currentEvent")?.GetValue(location)
                ?? location.GetType().GetProperty("currentEvent")?.GetValue(location);
            if (currentEvent is null)
                return false;
            object? value = currentEvent.GetType().GetField(member)?.GetValue(currentEvent)
                ?? currentEvent.GetType().GetProperty(member)?.GetValue(currentEvent);
            return value is bool result && result;
        }
        catch
        {
            return false;
        }
    }

    private static readonly string[] Capabilities = {
        "move", "face", "use_tool", "interact", "select_slot",
        "dialogue_continue", "dialogue_choose", "close_menu",
        "chest_take", "chest_store", "shop_buy", "wait"
    };

    private void Register()
    {
        this.Post("/api/game-plugins/stardew_valley/state", new { state = new
        {
            client_id = this.clientId,
            session_id = this.sessionId,
            save_id = Constants.SaveFolderName,
            observation_seq = Interlocked.Increment(ref this.observationSeq),
            full_snapshot = false,
            integration = "smapi",
            game_version = Game1.version,
            mod_version = this.ModManifest.Version.ToString(),
            capabilities = Capabilities,
            world_ready = true
        } });
    }

    private void Emit(string type, object payload)
    {
        this.Post("/api/game-plugins/stardew_valley/events", new { type, payload });
    }

    private void PollAction()
    {
        BridgeConfig? current = this.bridge;
        string requestedSession = this.sessionId;
        if (current is null || requestedSession.Length == 0
            || Interlocked.CompareExchange(ref this.polling, 1, 0) != 0)
            return;
        _ = Task.Run(async () =>
        {
            try
            {
                using var request = new HttpRequestMessage(HttpMethod.Get,
                    current.BaseUrl + "/api/game-plugins/stardew_valley/commands/next?client_id="
                    + Uri.EscapeDataString(this.clientId) + "&session_id="
                    + Uri.EscapeDataString(requestedSession));
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", current.Token);
                using HttpResponseMessage response = await this.http.SendAsync(request).ConfigureAwait(false);
                if (!response.IsSuccessStatusCode)
                    return;
                using JsonDocument doc = JsonDocument.Parse(
                    await response.Content.ReadAsStringAsync().ConfigureAwait(false));
                if (doc.RootElement.TryGetProperty("client_id", out JsonElement client)
                    && client.GetString() == this.clientId
                    && doc.RootElement.TryGetProperty("session_id", out JsonElement session)
                    && session.GetString() == requestedSession
                    && this.sessionId == requestedSession
                    && doc.RootElement.TryGetProperty("action", out JsonElement action)
                    && action.ValueKind == JsonValueKind.Object)
                    this.commands.Enqueue((requestedSession, action.Clone()));
            }
            catch { /* Runtime can restart independently. */ }
            finally { Interlocked.Exchange(ref this.polling, 0); }
        });
    }

    private static bool InvokeGameMethod(object target, string method, params object[] arguments)
    {
        try
        {
            MethodInfo? match = target.GetType().GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                .FirstOrDefault(candidate => candidate.Name.Equals(method, StringComparison.OrdinalIgnoreCase)
                    && candidate.GetParameters().Length == arguments.Length);
            if (match is null) return false;
            match.Invoke(target, arguments);
            return true;
        }
        catch { return false; }
    }

    private bool CanChooseDialogue() => !Game1.eventUp
        && Game1.activeClickableMenu is DialogueBox box
        && box.isQuestion && !box.transitioning
        && box.responses is { Length: > 0 }
        && box.responseCC is not null
        && box.responseCC.Count == box.responses.Length;

    private object[] DialogueOptions()
    {
        if (!this.CanChooseDialogue() || Game1.activeClickableMenu is not DialogueBox box)
            return Array.Empty<object>();
        return box.responses.Select((response, index) => (object)new
        {
            index, key = response.responseKey, text = response.responseText
        }).ToArray();
    }

    private bool CanTransferChest() => !Game1.eventUp
        && Game1.activeClickableMenu is ItemGrabMenu menu
        && menu.source == ItemGrabMenu.source_chest
        && menu.ItemsToGrabMenu?.actualInventory is not null
        && menu.inventory?.actualInventory is not null
        && menu.heldItem is null;

    private object[] ChestItems()
    {
        if (!this.CanTransferChest() || Game1.activeClickableMenu is not ItemGrabMenu menu)
            return Array.Empty<object>();
        return menu.ItemsToGrabMenu.actualInventory.Select((item, slot) => item is null
            ? null : (object)new
            {
                slot, item_id = item.QualifiedItemId, name = item.DisplayName, count = item.Stack
            }).Where(item => item is not null).Take(36).ToArray()!;
    }

    private object[] ShopItems()
    {
        if (Game1.eventUp || Game1.activeClickableMenu is not ShopMenu shop
            || shop.currency != 0 || shop.readOnly || shop.safetyTimer > 0)
            return Array.Empty<object>();
        var result = new List<object>();
        for (int visible = 0; visible < shop.forSaleButtons.Count; visible++)
        {
            int index = shop.currentItemIndex + visible;
            if (index < 0 || index >= shop.forSale.Count || shop.forSale[index] is not Item item
                || !shop.itemPriceAndStock.TryGetValue(item, out ItemStockInformation? stock)
                || stock.Stock == 0)
                continue;
            result.Add(new
            {
                index, item_id = item.QualifiedItemId, name = item.DisplayName,
                price = stock.Price, stock = stock.Stock
            });
        }
        return result.ToArray();
    }

    private static int CountItem(IEnumerable<Item?> items, string itemId) => items
        .Where(item => item is not null && item.QualifiedItemId == itemId)
        .Sum(item => item!.Stack);

    private static bool ClickComponent(IClickableMenu menu, ClickableComponent component)
    {
        if (component.bounds.Width <= 0 || component.bounds.Height <= 0)
            return false;
        menu.receiveLeftClick(component.bounds.Center.X, component.bounds.Center.Y, false);
        return true;
    }

    private bool CanContinueDialogue() => !Game1.eventUp
        && Game1.activeClickableMenu is DialogueBox { isQuestion: false, transitioning: false };

    private bool CanCloseMenu()
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        bool supported = menu is GameMenu or ItemGrabMenu or ShopMenu
            || menu is DialogueBox { isQuestion: false, transitioning: false };
        return !Game1.eventUp && supported && menu!.readyToClose();
    }

    private void BeginAction(JsonElement action)
    {
        try
        {
            string id = action.GetProperty("action_id").GetString() ?? "";
            string name = action.GetProperty("name").GetString() ?? "";
            JsonElement parameters = action.GetProperty("parameters");
            if (id.Length == 0) return;
            if (name is not ("wait" or "close_menu" or "dialogue_continue"
                or "dialogue_choose" or "chest_take" or "chest_store" or "shop_buy")
                && (!Context.IsPlayerFree || !Context.CanPlayerMove))
            {
                this.RejectAction(action);
                return;
            }
            this.activeActionId = id;
            this.activeActionName = name;
            this.startX = Game1.player.Position.X;
            this.startY = Game1.player.Position.Y;
            this.startEnergy = Game1.player.Stamina;
            this.startMenu = Game1.activeClickableMenu?.GetType().Name;
            this.startLocation = Game1.currentLocation?.NameOrUniqueName;
            this.startSpeaker = Game1.currentSpeaker?.Name;
            this.interactionAccepted = false;
            this.questionMenu = null;
            this.transferMenu = null;
            this.transferItemId = null;
            this.purchaseItemId = null;
            this.startItemCount = Game1.player.Items.Where(item => item is not null)
                .Sum(item => item!.Stack);
            if (name == "select_slot")
            {
                int slot = parameters.GetProperty("slot").GetInt32();
                if (slot < 0 || slot >= Game1.player.Items.Count)
                {
                    this.CompleteAction(false);
                    return;
                }
                Game1.player.CurrentToolIndex = slot;
                this.CompleteAction(Game1.player.CurrentToolIndex == slot);
                return;
            }
            if (name == "close_menu")
            {
                IClickableMenu? menu = Game1.activeClickableMenu;
                if (menu is null || !this.CanCloseMenu())
                {
                    this.CompleteAction(false);
                    return;
                }
                menu.exitThisMenu(false);
                this.CompleteAction(!ReferenceEquals(Game1.activeClickableMenu, menu));
                return;
            }
            if (name == "dialogue_continue")
            {
                if (!this.CanContinueDialogue() || Game1.activeClickableMenu is not DialogueBox box)
                {
                    this.CompleteAction(false);
                    return;
                }
                string text = box.getCurrentString();
                int character = box.characterIndexInDialogue;
                box.receiveLeftClick(box.xPositionOnScreen + box.width / 2,
                    box.yPositionOnScreen + box.height / 2, false);
                this.CompleteAction(!ReferenceEquals(Game1.activeClickableMenu, box)
                    || box.getCurrentString() != text || box.characterIndexInDialogue != character);
                return;
            }
            if (name == "dialogue_choose")
            {
                if (!this.CanChooseDialogue() || Game1.activeClickableMenu is not DialogueBox box)
                {
                    this.CompleteAction(false);
                    return;
                }
                int index = parameters.GetProperty("index").GetInt32();
                if (index < 0 || index >= box.responses.Length)
                {
                    this.CompleteAction(false);
                    return;
                }
                this.questionMenu = box;
                this.questionText = box.getCurrentString();
                this.actionTicks = 3;
                if (!ClickComponent(box, box.responseCC[index]))
                    this.CompleteAction(false);
                return;
            }
            if (name is "chest_take" or "chest_store")
            {
                if (!this.CanTransferChest() || Game1.activeClickableMenu is not ItemGrabMenu chest)
                {
                    this.CompleteAction(false);
                    return;
                }
                bool take = name == "chest_take";
                InventoryMenu source = take ? chest.ItemsToGrabMenu : chest.inventory;
                InventoryMenu destination = take ? chest.inventory : chest.ItemsToGrabMenu;
                int slot = parameters.GetProperty("slot").GetInt32();
                if (slot < 0 || slot >= source.actualInventory.Count
                    || slot >= source.inventory.Count || source.actualInventory[slot] is not Item item)
                {
                    this.CompleteAction(false);
                    return;
                }
                this.transferMenu = chest;
                this.transferItemId = item.QualifiedItemId;
                this.transferSourceCount = CountItem(source.actualInventory, this.transferItemId);
                this.transferDestinationCount = CountItem(destination.actualInventory, this.transferItemId);
                this.actionTicks = 3;
                if (!ClickComponent(chest, source.inventory[slot]))
                    this.CompleteAction(false);
                return;
            }
            if (name == "shop_buy")
            {
                if (Game1.eventUp || Game1.activeClickableMenu is not ShopMenu shop
                    || shop.currency != 0 || shop.readOnly || shop.safetyTimer > 0
                    || shop.heldItem is not null)
                {
                    this.CompleteAction(false);
                    return;
                }
                int index = parameters.GetProperty("index").GetInt32();
                int visible = index - shop.currentItemIndex;
                if (index < 0 || index >= shop.forSale.Count
                    || visible < 0 || visible >= shop.forSaleButtons.Count
                    || shop.forSale[index] is not Item item
                    || !shop.itemPriceAndStock.TryGetValue(item, out ItemStockInformation? stock)
                    || stock.Stock == 0 || stock.Price < 0 || Game1.player.Money < stock.Price)
                {
                    this.CompleteAction(false);
                    return;
                }
                this.purchaseItemId = item.QualifiedItemId;
                this.purchaseItemCount = CountItem(Game1.player.Items, this.purchaseItemId);
                this.purchaseMoney = Game1.player.Money;
                this.purchasePrice = stock.Price;
                this.actionTicks = 3;
                if (!ClickComponent(shop, shop.forSaleButtons[visible]))
                    this.CompleteAction(false);
                return;
            }
            if (name is "move" or "face")
            {
                string direction = parameters.GetProperty("direction").GetString() ?? "";
                int facing = direction switch { "up" => 0, "right" => 1, "down" => 2,
                    "left" => 3, _ => -1 };
                if (facing < 0) { this.CompleteAction(false); return; }
                if (name == "face")
                {
                    Game1.player.faceDirection(facing);
                    this.CompleteAction(Game1.player.FacingDirection == facing);
                    return;
                }
                this.actionTicks = Math.Clamp(parameters.GetProperty("ticks").GetInt32(), 1, 30);
                switch (direction)
                {
                    case "up": Game1.player.SetMovingUp(true); break;
                    case "down": Game1.player.SetMovingDown(true); break;
                    case "left": Game1.player.SetMovingLeft(true); break;
                    case "right": Game1.player.SetMovingRight(true); break;
                }
            }
            else if (name == "use_tool")
            {
                this.actionTicks = 12;
                if (!Game1.pressUseToolButton()) this.CompleteAction(false);
            }
            else if (name == "interact")
            {
                this.actionTicks = 5;
                int x = Game1.player.TilePoint.X + (Game1.player.FacingDirection == 1 ? 1
                    : Game1.player.FacingDirection == 3 ? -1 : 0);
                int y = Game1.player.TilePoint.Y + (Game1.player.FacingDirection == 2 ? 1
                    : Game1.player.FacingDirection == 0 ? -1 : 0);
                this.interactionAccepted = Game1.currentLocation?.checkAction(
                    new Location(x, y), Game1.viewport, Game1.player) ?? false;
                if (!this.interactionAccepted) this.CompleteAction(false);
            }
            else if (name == "wait")
            {
                this.actionTicks = Math.Clamp(parameters.GetProperty("ticks").GetInt32(), 1, 60);
            }
            else this.CompleteAction(false);
        }
        catch { this.CompleteAction(false); }
    }

    private void RejectAction(JsonElement action)
    {
        try
        {
            this.activeActionId = action.GetProperty("action_id").GetString();
            this.CompleteAction(false);
        }
        catch { /* Invalid commands are ignored; runtime times out without replay. */ }
    }

    private void AdvanceAction()
    {
        if (this.activeActionId is null || --this.actionTicks > 0) return;
        bool changed = this.activeActionName switch
        {
            "move" => Math.Abs(Game1.player.Position.X - this.startX) > 2
                || Math.Abs(Game1.player.Position.Y - this.startY) > 2,
            "interact" => this.interactionAccepted && (
                Game1.activeClickableMenu?.GetType().Name != this.startMenu
                || Game1.currentLocation?.NameOrUniqueName != this.startLocation
                || Game1.currentSpeaker?.Name != this.startSpeaker
                || Game1.player.Items.Where(item => item is not null).Sum(item => item!.Stack)
                    != this.startItemCount),
            "dialogue_choose" => this.questionMenu is not null
                && (!ReferenceEquals(Game1.activeClickableMenu, this.questionMenu)
                    || !this.questionMenu.isQuestion
                    || this.questionMenu.getCurrentString() != this.questionText),
            "chest_take" or "chest_store" => this.VerifyChestTransfer(),
            "shop_buy" => this.purchaseItemId is not null
                && CountItem(Game1.player.Items, this.purchaseItemId) > this.purchaseItemCount
                && Game1.player.Money <= this.purchaseMoney - this.purchasePrice,
            "wait" => true,
            _ => Game1.player.Stamina < this.startEnergy
        };
        this.CompleteAction(changed);
    }

    private bool VerifyChestTransfer()
    {
        if (this.transferMenu is not ItemGrabMenu menu || this.transferItemId is null)
            return false;
        bool take = this.activeActionName == "chest_take";
        InventoryMenu source = take ? menu.ItemsToGrabMenu : menu.inventory;
        InventoryMenu destination = take ? menu.inventory : menu.ItemsToGrabMenu;
        return CountItem(source.actualInventory, this.transferItemId) < this.transferSourceCount
            && CountItem(destination.actualInventory, this.transferItemId)
                > this.transferDestinationCount;
    }

    private void CompleteAction(bool verified)
    {
        string? id = this.activeActionId;
        if (id is null) return;
        if (this.activeActionName == "move")
            Game1.player.Halt();
        this.activeActionId = null;
        this.activeActionName = null;
        this.actionTicks = 0;
        this.questionMenu = null;
        this.transferMenu = null;
        this.transferItemId = null;
        this.purchaseItemId = null;
        this.lastAction = new { action_id = id, status = verified ? "verified" : "unverified" };
        this.Post("/api/game-plugins/stardew_valley/commands/receipt", new
        {
            action_id = id,
            verified,
            state = new
            {
                client_id = this.clientId,
                session_id = this.sessionId,
                save_id = Constants.SaveFolderName,
                observation_seq = Interlocked.Increment(ref this.observationSeq),
                full_snapshot = false,
                world_ready = true,
                location = Game1.currentLocation?.NameOrUniqueName,
                menu = Game1.activeClickableMenu?.GetType().Name ?? "gameplay",
                dialogue_text = (Game1.activeClickableMenu as DialogueBox)?.getCurrentString(),
                dialogue_question = (Game1.activeClickableMenu as DialogueBox)?.isQuestion ?? false,
                dialogue_can_continue = this.CanContinueDialogue(),
                dialogue_can_choose = this.CanChooseDialogue(),
                dialogue_options = this.DialogueOptions(),
                chest_can_transfer = this.CanTransferChest(),
                chest_items = this.ChestItems(),
                shop_items = this.ShopItems(),
                shop_currency = (Game1.activeClickableMenu as ShopMenu)?.currency,
                menu_closable = this.CanCloseMenu(),
                inventory = Game1.player.Items.Select((item, slot) => item is null ? null
                    : new { slot, item_id = item.QualifiedItemId, name = item.DisplayName,
                        count = item.Stack }).Where(item => item is not null).Take(36).ToArray(),
                player = new
                {
                    money = Game1.player.Money,
                    tile = new { x = Game1.player.TilePoint.X, y = Game1.player.TilePoint.Y },
                    energy = Game1.player.Stamina,
                    health = Game1.player.health,
                    selected_slot = Game1.player.CurrentToolIndex
                },
                last_action = this.lastAction
            }
        });
    }

    private void Post(string path, object payload)
    {
        BridgeConfig? current = this.bridge;
        if (current is null)
            return;
        // Capture game values and submission order before leaving the game thread.
        string body = JsonSerializer.Serialize(payload);
        _ = this.postQueue.Enqueue(async () =>
        {
            if (!ReferenceEquals(this.bridge, current))
                return;
            try
            {
                using var request = new HttpRequestMessage(HttpMethod.Post, current.BaseUrl + path);
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", current.Token);
                request.Content = new StringContent(body, Encoding.UTF8, "application/json");
                using HttpResponseMessage response = await this.http.SendAsync(request).ConfigureAwait(false);
                // A stale state/expired receipt is a rejected payload, not a broken
                // endpoint. Keep 422/409 and transient server responses connected.
                if (response.StatusCode is System.Net.HttpStatusCode.Unauthorized
                    or System.Net.HttpStatusCode.Forbidden or System.Net.HttpStatusCode.NotFound)
                    Interlocked.CompareExchange(ref this.bridge, null, current);
            }
            catch
            {
                // Clear only the endpoint used by this request, never a newer connection.
                Interlocked.CompareExchange(ref this.bridge, null, current);
            }
        });
    }

}

internal sealed record BridgeConfig(string BaseUrl, string Token)
{
    public static BridgeConfig? TryLoad()
    {
        try
        {
            string? configuredData = Environment.GetEnvironmentVariable("ELLA_DATA_DIR");
            string dataDirectory = string.IsNullOrWhiteSpace(configuredData)
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "EllaNext")
                : Path.GetFullPath(configuredData);
            string path = Path.Combine(dataDirectory, "game-bridge", "stardew_valley.json");
            if (!File.Exists(path))
                return null;
            using JsonDocument doc = JsonDocument.Parse(File.ReadAllText(path));
            string? token = doc.RootElement.GetProperty("token").GetString();
            if (token is null || token.Length < 20)
                return null;
            string baseUrl = doc.RootElement.TryGetProperty("base_url", out JsonElement endpoint)
                ? endpoint.GetString() ?? "" : "http://127.0.0.1:8766";
            if (!Uri.TryCreate(baseUrl, UriKind.Absolute, out Uri? uri)
                || uri.Scheme != "http" || uri.Host != "127.0.0.1"
                || uri.Port < 1 || !string.IsNullOrEmpty(uri.UserInfo)
                || uri.AbsolutePath != "/" || uri.Query != "" || uri.Fragment != "")
                return null;
            return new BridgeConfig(baseUrl.TrimEnd('/'), token);
        }
        catch
        {
            return null;
        }
    }
}
