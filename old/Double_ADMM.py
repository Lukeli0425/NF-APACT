import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.fft import fftn, fftshift, ifftn, ifftshift

from models.pact import PSF_PACT
from models.ResUNet import ResUNet
from utils.utils_torch import conv_fft_batch, get_fourier_coord, psf_to_otf


class DoubleConv(nn.Module):
    """(convolution => [BN] => ReLU) * 2"""
    def __init__(self, in_channels, out_channels, mid_channels=None):
        super(DoubleConv, self).__init__()
        if not mid_channels:
            mid_channels = out_channels
        self.double_conv = nn.Sequential(
            nn.Conv2d(in_channels, mid_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(mid_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(mid_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def forward(self, x):
        return self.double_conv(x)


class Down(nn.Module):
    """Downscaling with maxpool then double conv"""
    def __init__(self, in_channels, out_channels):
        super(Down, self).__init__()
        self.maxpool_conv = nn.Sequential(
            nn.MaxPool2d(2),
            DoubleConv(in_channels, out_channels)
        )

    def forward(self, x):
        return self.maxpool_conv(x)


class SubNet(nn.Module):
    def __init__(self, n_iters=8):
        super(SubNet, self).__init__()
        self.n_iters = n_iters
        self.cnn = nn.Sequential(
            Down(8,8),
            Down(8,16),
            Down(16,32),
            Down(32,32)
        )
        self.mlp = nn.Sequential(
            nn.Linear(32*8*8, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, 64),
            nn.ReLU(inplace=True),
            nn.Linear(64, 2*self.n_iters),
            nn.Softplus()
        )
        
    def forward(self, y):
        B, _, h, w  = y.size()
        H = fftn(y, dim=[-2,-1])
        HtH = torch.abs(H) ** 2
        x = self.cnn(HtH.float())
        x = x.view(B, 1, 32*8*8)
        output = self.mlp(x) + 1e-6

        rho1_iters = output[:,:,0:self.n_iters].view(B, 1, 1, self.n_iters)
        rho2_iters = output[:,:,self.n_iters:2*self.n_iters].view(B, 1, 1, self.n_iters).repeat(1,8,1,1)
        
        return rho1_iters, rho2_iters


class X_Update(nn.Module):
    def __init__(self):
        super(X_Update, self).__init__()
        
    def forward(self, Ht, y, HtH, z, u1, rho1):
        rhs = (Ht * fftn(y, dim=[-2,-1])).sum(axis=1).unsqueeze(1) + rho1 * fftn(z - u1, dim=[-2,-1])
        lhs = HtH.sum(axis=1).unsqueeze(1) + rho1
        x = ifftshift(ifftn(rhs/lhs, dim=[-2,-1]), dim=[-2,-1]).real

        return x
    
    
class Z_Update_ResUNet(nn.Module):
    """Updating Z with ResUNet as denoiser."""
    def __init__(self):
        super(Z_Update_ResUNet, self).__init__() 
        self.net = ResUNet(in_nc=1, out_nc=1, nc=[16, 32, 64, 128])

    def forward(self, z):
        z_out = self.net(z.float())
        return z_out


class H_Update(nn.Module):
    def __init__(self):
        super(H_Update, self).__init__()
        
    def forward(self, Xt, y, XtX, g, u2, rho2):
        rhs = (Xt * fftn(y, dim=[-2,-1])).sum(axis=1).unsqueeze(1) + rho2 * fftn(g - u2, dim=[-2,-1])
        lhs = XtX + rho2
        x = ifftshift(ifftn(rhs/lhs, dim=[-2,-1]), dim=[-2,-1])
        return x.real


class G_Update_ResUNet(nn.Module):
    """Updating G with ResUNet as denoiser."""
    def __init__(self):
        super(G_Update_ResUNet, self).__init__() 
        self.net = ResUNet(in_nc=8, out_nc=8, nc=[8, 16, 32, 64])

    def forward(self, g):
        g_out = self.net(g.float())
        return g_out


class G_Update_CNN(nn.Module):
    """Updating G with CNN and forward model."""
    def __init__(self, n_delays=8, device='cpu'):
        super(G_Update_CNN, self).__init__() 
        self.cnn = nn.Sequential(
            Down(8,8),
            Down(8,8),
            Down(8,16),
            Down(16,16)
        )
        self.mlp = nn.Sequential(
            nn.Linear(16*5*5, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, 64),
            nn.ReLU(inplace=True),
            nn.Linear(64, 6),
            nn.Softplus()
        )
        self.psf = PSF_PACT(n_points=80, n_delays=n_delays, device=device)
        # self.psf_filter = torch.ones([1,n_delays,64,64], device=device, requires_grad=True)
        # self.psf_bias = torch.ones([1,n_delays,64,64], device=device, requires_grad=True)

    def forward(self, h0):
        N = h0.shape[0] # Batch size.
        # print(h0.shape)
        # PSF parameter estimation.
        H = fftn(h0, dim=[-2,-1])
        HtH = torch.abs(H) ** 2
        x = self.cnn(HtH.float())
        x = x.view(N, 1, 16*5*5)
        params = self.mlp(x) + 1e-6
        params = params.repeat(1,8,1).unsqueeze(-1)
        
        # PSF reconstruction.
        g_out = self.psf(C0=params[:,:,0:1,:], C1=params[:,:,1:2,:], phi1=params[:,:,2:3,:], C2=params[:,:,3:4,:], phi2=params[:,:,4:5,:], offset=params[:,:,5:,:]) # * self.psf_filter + self.psf_bias * 1e-5

        return g_out


class Double_ADMM(nn.Module):
    def __init__(self, n_iters=8, n_delays=8):
        super(Double_ADMM, self).__init__()
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.n = n_iters # Number of iterations.
        self.n_delays = n_delays # Number of delays.
        self.X = X_Update() # FFT based quadratic solution.
        self.Z = Z_Update_ResUNet() # Denoiser.
        self.H = H_Update()
        self.G = G_Update_CNN(n_delays=self.n_delays, device=self.device) # Model-based PSF denoiser.
        # self.SubNet = SubNet(self.n)
        self.rho1_iters = nn.Parameter(torch.ones(size=(self.n,), requires_grad=True, device=self.device) * 0.25)
        self.rho2_iters = nn.Parameter(torch.ones(size=(self.n,), requires_grad=True, device=self.device) * 0.25)

    def init(self, y, psf):
        # B = y.shape[0] # Batch size.
        x = y[:,3:4,:,:]
        # psf_pact = PSF_PACT(n_delays=self.n_delays, device=self.device)
        # h = psf_pact(C0=7.5e-4 * torch.ones([B,self.n_delays,1,1], device=self.device), 
        #              C1=2e-5 * torch.ones([B,self.n_delays,1,1], device=self.device), 
        #              phi1=1e-3 * torch.ones([B,self.n_delays,1,1], device=self.device), 
        #              C2=2e-5 * torch.ones([B,self.n_delays,1,1], device=self.device), 
        #              phi2=1e-3 * torch.ones([B,self.n_delays,1,1], device=self.device)) + 1e-6
        return x, psf
        
    def forward(self, y, psf):
        
        B, _, H, W = y.size()
        
        x, h = self.init(y, psf) # Initialization.
        # rho1_iters, rho2_iters = self.SubNet(y) 	# Hyperparameters.
        
        # Other ADMM variables.
        z = Variable(x.data.clone()).to(self.device)
        g = Variable(h.data.clone()).to(self.device)
        u1 = torch.zeros(x.size()).to(self.device)
        u2 = torch.zeros(h.size()).to(self.device)
		
        # ADMM iterations
        for n in range(self.n):
            _, H, Ht, HtH = psf_to_otf(h)
            
            # rho1 = rho1_iters[:,:,:,n].view(B,1,1,1)
            # rho2 = rho2_iters[:,:,:,n].view(B,8,1,1)
            rho1, rho2 = self.rho1_iters[n], self.rho2_iters[n]
            
            # X, Z, H, G updates.
            z = self.Z(x + u1)
            x = self.X(Ht=Ht, y=y, HtH=HtH, z=z, u1=u1, rho1=rho1)
            
            
            _, X, Xt, XtX = psf_to_otf(x)
            g = self.G(h + u2)
            h = self.H(Xt=Xt, y=y, XtX=XtX, g=g, u2=u2, rho2=rho2)
            

            # Lagrangian dual variable updates.
            u1 = u1 + x - z            
            u2 = u2 + h - g

        return x #, h



if __name__ == '__main__':
    model = Double_ADMM(n_iters=4)
    total = sum([param.nelement() for param in model.parameters()])
    print("Number of parameter: %s" % (total))