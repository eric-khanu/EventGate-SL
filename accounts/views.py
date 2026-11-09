from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView

from .forms import SignUpForm


class SignUpView(CreateView):
    """Public sign-up. Always creates a plain viewer."""

    form_class = SignUpForm
    template_name = "accounts/signup.html"
    success_url = reverse_lazy("events:home")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)   # saves user, sets self.object
        login(self.request, self.object)      # log them straight in
        messages.success(self.request, "Welcome to EventGate SL!")
        return response


# accounts/views.py
# accounts/views.py
from django.contrib.auth.views import LoginView
from django.urls import reverse_lazy

from .permissions import is_scanner_only 


class EventGateLoginView(LoginView):
    template_name = "accounts/login.html"

    def get_success_url(self):
        user = self.request.user
        if is_scanner_only(user):
            return reverse_lazy("tickets:scanner")
        return super().get_success_url()

class EventGateLogoutView(LogoutView):
    """
    POST-only logout (Django >= 5.0 behaviour, made explicit).

    The logout button in ``base.html`` is a POST form with ``{% csrf_token %}``,
    so GET-based logout (which is a CSRF hole) is impossible.
    """

    next_page = reverse_lazy("events:home")
    http_method_names = ["post", "options"]